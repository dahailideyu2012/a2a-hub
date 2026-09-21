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
"""

from __future__ import annotations

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
from .social import USER, SocialHub

log = logging.getLogger("a2a_hub.rpc")


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
    ) -> None:
        self.registry = registry
        self.orchestrator = orchestrator
        # 会话层可选注入；不传就自带一个，方便单测与嵌入式使用
        self.social = social or SocialHub(registry, registry.bus)

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    async def handle(
        self, request: dict[str, Any], scoped_agent: Optional[str] = None
    ) -> dict[str, Any]:
        """处理一个 JSON-RPC 请求，返回响应 dict。"""
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

    def _new_task(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> tuple[Task, Message, str]:
        agent_id = self._resolve_agent(params, scoped_agent)
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
        run = self.orchestrator.create_run(
            mode, prompt, agent_ids, params.get("options") or {}
        )
        if params.get("blocking", True):
            await self.orchestrator.run_sync(run)
        else:
            await self.orchestrator.launch(run)
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
        items = self.social.contacts()
        return {"count": len(items), "contacts": items}

    async def _m_im_conversations(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        items = self.social.list_conversations()
        return {"count": len(items), "conversations": items}

    async def _m_im_open(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """打开（或复用）与某位 agent 的单聊。"""
        agent_id = _require(params, "agentId")
        if not self.registry.has(agent_id):
            raise A2AError(
                JsonRpcErrorCodes.INVALID_PARAMS,
                f"未找到 agent `{agent_id}`",
                {"available": [r.id for r in self.registry.list_records()]},
            )
        return self.social.summary(self.social.open_direct(agent_id))

    async def _m_im_group(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> dict[str, Any]:
        """建群。"""
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
            )
        except ValueError as exc:
            raise A2AError(JsonRpcErrorCodes.INVALID_PARAMS, str(exc)) from exc
        return self.social.summary(conv)

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
                sender=params.get("sender") or USER,
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
        return self.social.history(conv_id, int(params.get("limit") or 200))

    async def _m_im_events(
        self, params: dict[str, Any], scoped_agent: Optional[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """会话事件流（流式方法，上层会包装成 SSE）。"""
        conv_id = _require(params, "conversationId")
        self._conv(conv_id)
        async for event in self.social.stream(conv_id):
            yield event


# 兼容：JsonRpcError 从 models 引入便于外部使用
from .models import JsonRpcError  # noqa: E402  (置于文件末尾避免循环引用困扰)

__all__ = ["JsonRpcDispatcher"]
