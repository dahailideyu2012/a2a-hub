"""JSON-RPC 2.0 分发层 —— A2A 协议的方法实现。

方法清单（对齐 A2A v0.3）：
  message/send                     提交消息，返回 Task 或 Message
  message/stream                   提交消息，SSE 流式返回 Task/事件
  tasks/get                        查询任务
  tasks/cancel                     取消任务
  tasks/resubscribe                重连到任务的 SSE 事件流
  tasks/pushNotificationConfig/*   推送通知（本实现返回不支持，属可选能力）

Hub 扩展方法（非 A2A 标准，用于多 agent 协同）：
  agents/list                      列出全部 agent 及其健康状态
  agents/card                      查询单个 agent 的 Agent Card
  agents/health                    触发健康探测
  collab/run                       发起一次多 agent 协同（返回 runId）
  collab/get                       查询协同运行详情
  collab/modes                     列出可用协同模式
  hub/capabilities                 本 Hub 的对外能力清单（CLI/HTTP/MCP 三种接法）

社交层（Hub 扩展，让 agent 能自己打理关系）：
  social/me                        我的名片与关系概览
  social/members                   搜索可发现成员
  social/profile                   看别人的名片（只有共同好友数，无名单）
  social/discover                  自主发现：按匹配度推荐 + 打分明细
  social/introductions             别人引荐给我的（收件箱式）
  social/introduce                 引荐（不授予任何 scope）
  social/relations                 我的关系列表
  social/requests                  待我处理 / 我发出的申请
  social/request                   发起好友申请
  social/accept                    同意申请
  social/reject                    拒绝申请
  social/grant                     调整某位好友的 scope
  social/revoke                    删好友
  social/block                     拉黑 / 解除

自主交友（§6，需要成员声明 `autonomy`）：
  social/need                      报告能力缺口 → 发现 → 申请 / 挂 owner 待办
  social/pending                   需要我拍板的待办
  social/approve                   批准待办（越界自主行为成真的唯一路径）
  social/deny                      驳回待办
"""

from __future__ import annotations

import contextvars
import inspect
import logging
from typing import Any, AsyncIterator, Optional

from .models import (
    A2AError,
    JsonRpcError,
    JsonRpcErrorCodes,
    JsonRpcRequest,
    JsonRpcResponse,
    Message,
    Task,
    TaskState,
    parse_part,
)
from .orchestrator import MODES, Orchestrator
from .registry import AgentRegistry
from .relations import Scope, SocialError, SocialGraph
from .social import USER, SocialHub

log = logging.getLogger("a2a_hub.rpc")

#: 当前请求的调用者。用 contextvar 而不是实例属性，是为了让
#: ``JsonRpcDispatcher`` 保持**无状态**（它被文档描述为无状态分发器，
#: 塞一个 self.actor 进去会让并发请求互相串身份）。
#:
#: 注意：流式方法（``message/stream``）会把它交给 SSE 任务再被迭代，
#: 届时 contextvar 未必还在。**需要身份的流式方法必须显式传参**，
#: 不要依赖这个默认值。
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("a2a_actor", default=USER)


def current_actor() -> str:
    """当前调用者的成员 id。未经鉴权时退化为 ``USER``。"""
    return _ACTOR.get()


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, f"缺少必需参数 `{key}`")
    return params[key]


def _build_message(raw: Any) -> Message:
    """把入参 message 规范化。既接受完整 A2A Message，也接受裸字符串。"""
    if isinstance(raw, str):
        return Message.user(raw)
    if not isinstance(raw, dict):
        raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, "`message` 必须是对象或字符串")
    parts_raw = raw.get("parts")
    if parts_raw is None:
        text = raw.get("text") or raw.get("content") or ""
        parts = [parse_part(text)]
    else:
        parts = [parse_part(p) for p in parts_raw]
    return Message(
        role=raw.get("role", "user"),
        parts=parts,
        messageId=raw.get("messageId") or Message.model_fields["messageId"].default_factory(),
        taskId=raw.get("taskId"),
        contextId=raw.get("contextId"),
        metadata=raw.get("metadata") or {},
    )


class JsonRpcDispatcher:
    """无状态分发器：路由 + 参数校验 + 异常 -> JSON-RPC 错误映射。"""

    def __init__(
        self,
        registry: AgentRegistry,
        orchestrator: Orchestrator,
        social: Optional[SocialHub] = None,
        graph: Optional[SocialGraph] = None,
    ) -> None:
        self.registry = registry
        self.orchestrator = orchestrator
        # 会话层可选注入；不传就自带一个，方便单测与嵌入式使用
        self.social = social or SocialHub(registry, registry.bus)
        #: 关系图。没注入时 `social/*` 方法一律返回 `-32008` 而不是假装成功。
        self.graph = graph

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    async def handle(
        self,
        request: dict[str, Any],
        scoped_agent: Optional[str] = None,
        actor: str = USER,
    ) -> dict[str, Any]:
        """处理一个 JSON-RPC 请求，返回响应 dict。

        ``actor`` 由 **HTTP 层从鉴权结果**传入，**绝不从请求体里取**——
        请求体是客户端可以随便写的。
        """
        _ACTOR.set(actor)
        req_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return JsonRpcResponse(
                id=req_id,
                error=JsonRpcError(
                    code=JsonRpcErrorCodes.INVALID_REQUEST,
                    message="非法 JSON-RPC 请求：需要 jsonrpc='2.0' 与 method 字段",
                ),
            ).model_dump(exclude_none=True)

        try:
            validated = JsonRpcRequest(**request)
        except Exception as exc:  # noqa: BLE001
            return JsonRpcResponse(
                id=req_id,
                error=JsonRpcError(code=JsonRpcErrorCodes.INVALID_REQUEST, message=str(exc)),
            ).model_dump(exclude_none=True)

        try:
            result = await self.dispatch(validated.method, validated.params, scoped_agent)
            # 流式方法返回异步生成器，交给上层包装成 SSE，不能塞进 JSON-RPC 信封
            if hasattr(result, "__aiter__"):
                return result  # type: ignore[return-value]
            if isinstance(result, JsonRpcResponse):
                return result.model_dump(exclude_none=True)
            return JsonRpcResponse(id=req_id, result=result).model_dump(exclude_none=True)

        except A2AError as exc:
            return JsonRpcResponse(id=req_id, error=exc.to_rpc_error()).model_dump(exclude_none=True)
        except NotImplementedError as exc:
            return JsonRpcResponse(
                id=req_id,
                error=JsonRpcError(
                    code=JsonRpcErrorCodes.UNSUPPORTED_OPERATION, message=str(exc)
                ),
            ).model_dump(exclude_none=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("JSON-RPC 方法 %s 执行异常", validated.method)
            return JsonRpcResponse(
                id=req_id,
                error=JsonRpcError(
                    code=JsonRpcErrorCodes.INTERNAL_ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                ),
            ).model_dump(exclude_none=True)

    async def dispatch(
        self, method: str, params: dict[str, Any], scoped_agent: Optional[str] = None
    ) -> Any:
        handler = getattr(self, "_m_" + method.replace("/", "_").replace(".", "_"), None)
        if handler is None:
            raise A2AError(
                JsonRpcErrorCodes.METHOD_NOT_FOUND,
                f"不支持的方法 `{method}`",
                {"supported": self.supported_methods()},
            )
        # 流式方法（message/stream、tasks/resubscribe）是异步生成器函数：
        # 调用它得到的是一个 async generator，**不能 await**，直接返回给上层包 SSE。
        if inspect.isasyncgenfunction(handler):
            return handler(params or {}, scoped_agent)
        return await handler(params or {}, scoped_agent)

    @staticmethod
    def supported_methods() -> list[str]:
        return [
            "message/send",
            "message/stream",
            "tasks/get",
            "tasks/cancel",
            "tasks/resubscribe",
            "agents/list",
            "agents/card",
            "agents/health",
            "collab/run",
            "collab/get",
            "collab/modes",
            "im/contacts",
            "im/conversations",
            "im/open",
            "im/group",
            "im/send",
            "im/history",
            "im/events",
            "social/me",
            "social/members",
            "social/profile",
            "social/discover",
            "social/introductions",
            "social/introduce",
            "social/relations",
            "social/requests",
            "social/request",
            "social/accept",
            "social/reject",
            "social/grant",
            "social/revoke",
            "social/block",
            "social/pending",
            "social/approve",
            "social/deny",
            "social/need",
            "hub/capabilities",
        ]

    # ------------------------------------------------------------------ #
    # 参数解析
    # ------------------------------------------------------------------ #

    def _resolve_agent(self, params: dict[str, Any], scoped_agent: Optional[str]) -> str:
        agent_id = params.get("agentId") or scoped_agent
        if not agent_id:
            # 未指定则按能力自动路由
            message = params.get("message")
            query = _build_message(message).text() if message is not None else ""
            rec = self.registry.route(query)
            if rec is None:
                raise A2AError(
                    JsonRpcErrorCodes.INVALID_PARAMS,
                    "未指定 agentId 且自动路由失败（没有可用 agent）",
                    {"agents": [r.id for r in self.registry.list_records()]},
                )
            return rec.id
        if not self.registry.has(agent_id):
            raise A2AError(
                JsonRpcErrorCodes.INVALID_PARAMS,
                f"未找到 agent `{agent_id}`",
                {"available": [r.id for r in self.registry.list_records()]},
            )
        return agent_id

    def _require_delegate(self, actor: str, agent_id: str) -> None:
        """**二道门**：让某个 agent 干活，需要它给过 ``delegate``。

        「能聊天」≠「能指挥你干活」。IM 会话里发消息只要 ``chat``，
        而 ``message/send`` / ``collab/run`` 这类**消耗对方额度**的调用，
        必须额外拿到 ``delegate``——否则加个好友就等于让人替你加班。

        门禁没启用（无 ``members.yaml``）时直接放行，与 v0.3.0 一致。
        """
        g = self.graph
        if g is None or not g.enabled:
            return
        if g.delegable(actor, agent_id):
            return
        data = g.refusal_data(
            actor,
            agent_id,
            Scope.DELEGATE,
            hint=(
                f"先与 {g.normalize(agent_id)} 建立好友关系，"
                f'再 `PATCH /social/relations/{g.normalize(agent_id)} '
                '{"scopes":["peek","chat","invite","delegate"]}` 显式授予执行权'
            ),
        )
        raise A2AError(
            JsonRpcErrorCodes.SOCIAL_DENIED,
            f"社交门禁：`{actor}` 尚未获得 `{agent_id}` 的执行授权（delegate）",
            data,
        )

    def _require_delegate_for(self, actor: str, agent_ids: list[str]) -> None:
        """批量版 delegate 门禁：一次把**所有**派不动的 agent 报出来。

        逐个报会让调用方改一个试一次（N 次往返），一次报全反而更好用。
        """
        g = self.graph
        if g is None or not g.enabled or not agent_ids:
            return
        refused = g.refused_agents(actor, agent_ids)
        if not refused:
            return
        raise A2AError(
            JsonRpcErrorCodes.SOCIAL_DENIED,
            "社交门禁：以下 agent 尚未授予你执行权（delegate）："
            + "、".join(refused),
            {
                "peer": refused[0],
                "peers": refused,
                "needScope": Scope.DELEGATE.value,
                "reason": "missing_scope",
                "hint": "请对方（或其 owner）执行 PATCH /social/relations/<你> 授予 delegate",
            },
        )

    def _new_task(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> tuple[Task, Message, str]:
        agent_id = self._resolve_agent(params, scoped_agent)
        # **先过门禁再建任务**：反过来的话，被拒的请求已经落库了
        self._require_delegate(current_actor(), agent_id)
        message = _build_message(_require(params, "message"))
        context_id = params.get("contextId") or (params.get("configuration") or {}).get("contextId")
        task = self.registry.new_task(
            agent_id, message, context_id, params.get("metadata") or {}
        )
        return task, message, agent_id

    # ------------------------------------------------------------------ #
    # A2A 标准方法
    # ------------------------------------------------------------------ #

    async def _m_message_send(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        task, message, agent_id = self._new_task(params, scoped_agent)
        rec = self.registry.get(agent_id)
        configuration = params.get("configuration") or {}
        blocking = configuration.get("blocking", True)

        stream = self.registry.execute(rec, task, message)
        if blocking:
            async for _ in stream:
                pass
            return task.model_dump(mode="json")

        # 非阻塞：后台跑，立即返回当前（submitted）任务对象
        import asyncio

        async def drain() -> None:
            try:
                async for _ in stream:
                    pass
            except Exception:  # noqa: BLE001
                pass

        asyncio.create_task(drain())
        self.registry.store.save(task)
        return task.model_dump(mode="json")

    async def _m_message_stream(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> AsyncIterator[dict[str, Any]]:
        task, message, agent_id = self._new_task(params, scoped_agent)
        rec = self.registry.get(agent_id)
        async for event in self.registry.execute(rec, task, message):
            yield event.model_dump(mode="json")

    async def _m_tasks_get(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        task_id = _require(params, "id")
        task = self.registry.store.get(task_id)
        if task is None:
            raise A2AError(JsonRpcErrorCodes.TASK_NOT_FOUND, f"未找到任务 `{task_id}`")
        data = task.model_dump(mode="json")
        history_length = params.get("historyLength")
        if isinstance(history_length, int) and history_length >= 0:
            data["history"] = data["history"][-history_length:] if history_length else []
        return data

    async def _m_tasks_cancel(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        task = await self.registry.cancel(_require(params, "id"))
        return task.model_dump(mode="json")

    async def _m_tasks_resubscribe(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> AsyncIterator[dict[str, Any]]:
        task_id = _require(params, "id")
        task = self.registry.store.get(task_id)
        if task is None:
            raise A2AError(JsonRpcErrorCodes.TASK_NOT_FOUND, f"未找到任务 `{task_id}`")
        yield {"kind": "task", **task.model_dump(mode="json")}
        if task.status.is_terminal:
            return

        from .bus import STREAM_END

        bus = self.registry.bus
        q = bus.subscribe(task_id)
        try:
            while True:
                got, event = await bus.next_event(q, timeout=25.0)
                if not got:
                    current = self.registry.store.get(task_id)
                    if current is not None and current.status.is_terminal:
                        return
                    continue
                if event is STREAM_END:
                    return
                yield event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        finally:
            bus.unsubscribe(task_id, q)

    async def _m_tasks_pushNotificationConfig_set(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> Any:
        raise NotImplementedError("本实现未启用 pushNotification 能力（capabilities.pushNotifications=false）")

    async def _m_tasks_pushNotificationConfig_get(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> Any:
        raise NotImplementedError("本实现未启用 pushNotification 能力")

    # ------------------------------------------------------------------ #
    # Hub 扩展方法
    # ------------------------------------------------------------------ #

    async def _m_agents_list(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        if params.get("refresh"):
            await self.registry.check_health(force=True)
        return {
            "count": len(self.registry.list_records()),
            "agents": self.registry.snapshot(params.get("baseUrl")),
        }

    async def _m_hub_capabilities(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """``hub/capabilities`` —— 本 Hub 的对外能力清单（机器可读）。

        与 HTTP ``GET /capabilities``、CLI ``run.py capabilities`` **同源**，
        都来自 ``capabilities.CAPABILITIES``。新增能力时只改那一处。
        """
        from .capabilities import manifest

        return manifest(base_url=params.get("baseUrl") or "")

    async def _m_agents_card(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        agent_id = params.get("agentId") or scoped_agent
        if not agent_id:
            return self.registry.hub_card()
        return self.registry.agent_card(agent_id)

    async def _m_agents_health(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        results = await self.registry.check_health(force=bool(params.get("force", True)))
        return {"results": results}

    async def _m_collab_modes(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        return {
            "modes": [
                {
                    "id": "delegate",
                    "name": "委派",
                    "description": "按能力自动路由到最合适的单个 agent 执行，可选叠加评审 agent",
                    "options": ["reviewer", "topK"],
                },
                {
                    "id": "broadcast",
                    "name": "广播",
                    "description": "多个 agent 并行处理同一问题，再由综合者收敛成一份结论",
                    "options": ["topK", "synthesizer"],
                },
                {
                    "id": "pipeline",
                    "name": "流水线",
                    "description": "串行接力，上游产出作为下游输入（调研→实现→校核）",
                    "options": ["stages", "stagesCount", "synthesizer"],
                },
                {
                    "id": "roundtable",
                    "name": "圆桌",
                    "description": "多轮讨论，每轮 agent 看到他人观点后修订，最终收敛",
                    "options": ["rounds", "topK", "synthesizer"],
                },
            ]
        }

    async def _m_collab_run(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        mode = _require(params, "mode")
        prompt = _require(params, "prompt")
        if mode not in MODES:
            raise A2AError(
                JsonRpcErrorCodes.INVALID_PARAMS,
                f"不支持的协同模式 `{mode}`",
                {"available": list(MODES)},
            )
        agent_ids = params.get("agentIds") or ([await self._resolve_agent(params, scoped_agent)] if scoped_agent else [])
        actor = current_actor()
        # 显式点名的成员要过 delegate 门禁；自动路由的那部分由编排器自己过滤
        self._require_delegate_for(actor, agent_ids)
        run = self.orchestrator.create_run(
            mode, prompt, agent_ids, params.get("options") or {}, actor=actor
        )
        try:
            if params.get("blocking", True):
                await self.orchestrator.run_sync(run)
            else:
                await self.orchestrator.launch(run)
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        return run.model_dump(mode="json")

    async def _m_collab_get(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        run_id = _require(params, "runId")
        run = self.orchestrator.get_run(run_id)
        if run is None:
            raise A2AError(JsonRpcErrorCodes.TASK_NOT_FOUND, f"未找到协同运行 `{run_id}`")
        return run.model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # 会话层（IM）—— 把 agent 当"好友"聊
    # ------------------------------------------------------------------ #

    def _conv(self, conv_id: str) -> Any:
        try:
            return self.social.get(conv_id)
        except KeyError as exc:
            raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, str(exc)) from exc

    async def _m_im_contacts(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        actor = current_actor()
        items = self.social.contacts(actor)
        return {"count": len(items), "contacts": items, "me": actor}

    async def _m_im_conversations(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        items = self.social.list_conversations(current_actor())
        return {"count": len(items), "conversations": items}

    async def _m_im_open(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """打开（或复用）与某位 agent 的单聊。"""
        actor = current_actor()
        agent_id = _require(params, "agentId")
        if not self.registry.has(agent_id):
            raise A2AError(
                JsonRpcErrorCodes.INVALID_PARAMS,
                f"未找到 agent `{agent_id}`",
                {"available": [r.id for r in self.registry.list_records()]},
            )
        return self.social.summary(self.social.open_direct(agent_id, owner=actor), actor)

    async def _m_im_group(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """建群。"""
        actor = current_actor()
        members = params.get("members") or params.get("agentIds")
        if not members:
            raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, "`im/group` 需要 `members`")
        for mid in members:
            if not self.registry.has(mid):
                raise A2AError(
                    JsonRpcErrorCodes.INVALID_PARAMS, f"未找到 agent `{mid}`"
                )
        try:
            conv = self.social.create_group(
                list(members),
                params.get("title") or "",
                bool(params.get("autoRoute", True)),
                actor=actor,
            )
        except PermissionError as exc:
            raise A2AError(JsonRpcErrorCodes.SOCIAL_DENIED, str(exc)) from exc
        except ValueError as exc:
            raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, str(exc)) from exc
        return self.social.summary(conv, actor)

    async def _m_im_send(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """发一条消息，立即返回；对方的回复会作为新消息异步追加。"""
        conv_id = _require(params, "conversationId")
        text = _require(params, "text")
        self._conv(conv_id)
        try:
            return await self.social.send(
                conv_id,
                str(text),
                sender=current_actor(),
                reply_to=params.get("replyTo"),
                wake=params.get("wake"),
            )
        except ValueError as exc:
            raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, str(exc)) from exc

    async def _m_im_history(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        conv_id = _require(params, "conversationId")
        self._conv(conv_id)
        return self.social.history(
            conv_id, int(params.get("limit") or 200), viewer=current_actor()
        )

    async def _m_im_events(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """会话事件流（流式方法，上层会包装成 SSE）。"""
        conv_id = _require(params, "conversationId")
        self._conv(conv_id)
        async for event in self.social.stream(conv_id):
            yield event

    # ------------------------------------------------------------------ #
    # 社交层 —— 让 agent 能自己打理关系，而不是只能等人给它加好友
    # ------------------------------------------------------------------ #

    def _need_graph(self) -> SocialGraph:
        if self.graph is None or not self.graph.enabled:
            raise A2AError(
                JsonRpcErrorCodes.SOCIAL_DENIED,
                "社交层未启用（未找到 members.yaml，或 A2A_SOCIAL_MODE=off）",
                {
                    "reason": "social_disabled",
                    "hint": (
                        "执行 `python run.py social init`（MCP 里调 a2a_social_init）"
                        "即可一键生成 config/members.yaml 并热启用，无需重启"
                    ),
                },
            )
        return self.graph

    @staticmethod
    def _social_fail(exc: SocialError) -> A2AError:
        return A2AError(
            JsonRpcErrorCodes.SOCIAL_DENIED,
            str(exc),
            {"reason": type(exc).__name__, "hint": "POST /social/requests 建立好友关系"},
        )

    async def _social_actor(self, params: dict[str, Any]) -> str:
        """只读社交方法的「以谁的身份看」。

        owner 可以带 ``as=<自己的 agent>`` 看它的视角——否则 agent 收到的
        好友申请就没人能处理了。越权直接 ``-32008``，与写入侧共用同一条规则。
        """
        g = self._need_graph()
        actor = current_actor()
        who = params.get("as")
        if not who:
            return actor
        target = g.normalize(str(who))
        if target != actor and not g.can_act_for(actor, target):
            raise A2AError(
                JsonRpcErrorCodes.SOCIAL_DENIED,
                f"无权以 `{target}` 的身份查看",
            )
        return target

    async def _m_social_me(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        return self._need_graph().me(await self._social_actor(params))

    async def _m_social_members(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        g = self._need_graph()
        who = await self._social_actor(params)
        items = g.search(
            str(params.get("q") or ""),
            viewer=who,
            limit=int(params.get("limit") or 50),
        )
        return {"count": len(items), "members": items, "me": who}

    async def _m_social_profile(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """看别人的名片：**只有共同好友数，没有好友名单**。"""
        g = self._need_graph()
        who = await self._social_actor(params)
        return g.profile(str(_require(params, "member")), who)

    async def _m_social_discover(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """自主发现：按匹配度推荐值得认识的人，附打分明细。"""
        g = self._need_graph()
        who = await self._social_actor(params)
        need = str(params.get("need") or "")
        items = g.discover(who, need=need, limit=int(params.get("limit") or 10))
        return {"count": len(items), "candidates": items, "need": need, "me": who}

    async def _m_social_introductions(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        g = self._need_graph()
        who = await self._social_actor(params)
        items = g.introductions(who)
        return {"count": len(items), "introductions": items, "me": who}

    async def _m_social_introduce(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """引荐。只有同时是双方好友的人能做，且**不授予任何 scope**。"""
        g = self._need_graph()
        actor = current_actor()
        try:
            return g.introduce(
                actor,
                str(_require(params, "to")),
                str(_require(params, "peer")),
                str(params.get("note") or ""),
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc

    async def _m_social_relations(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        items = self._need_graph().relations_of(await self._social_actor(params))
        return {"count": len(items), "relations": items}

    async def _m_social_pending(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """需要我拍板的自主交友待办（§6 约束 2）。每条都写明命中了哪条策略。"""
        g = self._need_graph()
        who = await self._social_actor(params)
        items = g.pending_approvals(owner=who)
        return {"count": len(items), "pending": items, "me": who}

    async def _m_social_approve(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """批准一条待办。**这是越界自主行为成真的唯一路径。**"""
        g = self._need_graph()
        actor = current_actor()
        try:
            return g.approve_pending(
                actor, str(_require(params, "id")), params.get("scopes")
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc

    async def _m_social_deny(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """驳回一条待办。理由只进自己的审计，不告诉对方。"""
        g = self._need_graph()
        actor = current_actor()
        try:
            return g.deny_pending(
                actor, str(_require(params, "id")), str(params.get("reason") or "")
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc

    async def _m_social_need(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """报告能力缺口（§6.1 A）。由 Hub 去发现并按策略申请 / 挂 owner 待办。"""
        g = self._need_graph()
        who = await self._social_actor(params)
        return g.request_for_need(
            who,
            str(_require(params, "need")),
            reason=str(params.get("reason") or ""),
            limit=int(params.get("limit") or 3),
        )

    async def _m_social_requests(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        g = self._need_graph()
        actor = await self._social_actor(params)
        box = str(params.get("box") or "in")
        items = g.inbox(actor) if box == "in" else g.outbox(actor)
        return {"count": len(items), "box": box, "member": actor, "requests": items}

    async def _m_social_request(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """发起好友申请。``message`` 必填——理由既是防骚扰，也让对方知道你是谁。"""
        g = self._need_graph()
        actor = current_actor()
        to = _require(params, "to")
        try:
            rel = g.request(
                actor, str(to), str(params.get("message") or ""), params.get("scopes")
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        return g.describe(rel, actor)

    async def _m_social_accept(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """同意申请。``scopes`` 是**我实际授予对方**的范围，默认只给对话类。"""
        g = self._need_graph()
        actor = current_actor()
        try:
            rel = g.accept(
                actor,
                str(_require(params, "peer")),
                params.get("scopes"),
                as_member=params.get("as"),
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        acting = g.normalize(params.get("as") or actor)
        self.social.thaw_between(acting, g.normalize(str(_require(params, "peer"))))
        return g.describe(rel, actor)

    async def _m_social_reject(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        g = self._need_graph()
        actor = current_actor()
        try:
            rel = g.reject(
                actor,
                str(_require(params, "peer")),
                str(params.get("reason") or ""),
                as_member=params.get("as"),
            )
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        return g.describe(rel, actor)

    async def _m_social_grant(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """调权限。会强制检查权限上行闭包——不能授予自己没有的东西。"""
        g = self._need_graph()
        actor = current_actor()
        try:
            rel = g.set_grant(actor, str(_require(params, "peer")), params.get("scopes") or [])
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        return g.describe(rel, actor)

    async def _m_social_revoke(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        g = self._need_graph()
        actor = current_actor()
        peer = str(_require(params, "peer"))
        try:
            rel = g.revoke(actor, peer)
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        self.social.freeze_between(actor, g.normalize(peer))
        return g.describe(rel, actor)

    async def _m_social_block(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """拉黑 / 解除。``{"peer": "spammer", "unblock": true}`` 表示解除。"""
        g = self._need_graph()
        actor = current_actor()
        peer = str(_require(params, "peer"))
        try:
            rel = g.unblock(actor, peer) if params.get("unblock") else g.block(actor, peer)
        except SocialError as exc:
            raise self._social_fail(exc) from exc
        if not params.get("unblock"):
            self.social.freeze_between(actor, g.normalize(peer))
        else:
            self.social.thaw_between(actor, g.normalize(peer))
        return g.describe(rel, actor)


# 兼容：JsonRpcError 从 models 引入便于外部使用
from .models import JsonRpcError  # noqa: E402  (置于文件末尾避免循环引用困扰)

__all__ = ["JsonRpcDispatcher"]
