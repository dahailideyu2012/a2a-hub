"""FastAPI 应用 —— 暴露 A2A 端点、协同 API 与 Web 控制台。

端点地图
--------
发现层
  GET  /.well-known/agent.json               Hub 的 Agent Card
  GET  /.well-known/agent-card.json          同上（兼容写法）
  GET  /agents                                所有子 agent 快照
  GET  /agents/{id}/.well-known/agent.json    单个子 agent 的 Agent Card

A2A 协议层（JSON-RPC 2.0）
  POST /                                      Hub 入口，agentId 可缺省（自动路由）
  POST /agents/{id}/                          绑定到具体 agent 的入口

任务层
  GET  /tasks                                 最近任务
  GET  /tasks/{task_id}                       任务详情
  GET  /tasks/{task_id}/events                SSE 订阅任务事件

协同层
  POST /collab                                发起多 agent 协同
  GET  /collab                                历史协同列表
  GET  /collab/{run_id}                       协同详情
  GET  /collab/{run_id}/events                协同过程 SSE
  DELETE /collab/{run_id}                     取消协同

会话层（IM）—— 把 agent 当"好友"聊，而非一次性 RPC
  GET    /im/contacts                          通讯录（agent 列表 + 在线状态）
  GET    /im/conversations                     会话列表（含最后消息与未读数）
  POST   /im/conversations                     新建单聊 / 群聊
  GET    /im/conversations/{id}                聊天记录 + 投递回执
  POST   /im/conversations/{id}/messages       发消息（立即返回，后台回话）
  POST   /im/conversations/{id}/read           清未读
  PATCH  /im/conversations/{id}                群聊拉人 / 踢人
  DELETE /im/conversations/{id}                解散会话
  GET    /im/conversations/{id}/events         会话事件 SSE

社交层（/social/*）—— 成员、好友关系与交流权限
  GET    /social/me                            我的名片 + 我的边 + 待办数
  GET    /social/members                       搜索可发现成员
  GET    /social/relations                     我的关系列表
  POST   /social/requests                      发好友申请
  GET    /social/requests                      收件箱（我收到的申请）
  POST   /social/requests/accept               同意
  POST   /social/requests/reject               拒绝
  POST   /social/requests/cancel               撤回自己发的
  PATCH  /social/relations/{peer}              改权限（scope）
  DELETE /social/relations/{peer}              删好友
  POST   /social/relations/{peer}/block        拉黑 / 解除
  GET    /social/audit                         审计链
  GET    /social/events                        社交事件 SSE

控制台
  GET  /console                                Web 控制台
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .autonomy import SocialCruise
from .config import get_settings
from .models import A2AError, JsonRpcError, JsonRpcErrorCodes, JsonRpcResponse, utc_now
from .orchestrator import Orchestrator
from .registry import AgentRegistry, bootstrap
from .relations import (
    DEFAULT_MEMBER,
    USER,
    Member,
    RelationState,
    SocialError,
    SocialGraph,
    social_briefing,
)
from .capabilities import manifest
from .rpc import JsonRpcDispatcher
from .social_boot import bootstrap_members
from .social import SocialHub
from .store import task_to_wire

log = logging.getLogger("a2a_hub.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: 控制台用 Cookie 保存 Bearer token —— 浏览器导航时没法带 Authorization 头，
#: 而把 token 塞进 URL 会被访问日志、Referer 到处传播。
CONSOLE_COOKIE = "a2a_token"

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关键：避免 Nginx 缓冲把 SSE 憋住
}


def sse_format(data: Any, event: Optional[str] = None) -> str:
    """序列化为一条 SSE 报文。"""
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, default=str)
    payload = data.replace("\r\n", "\n")
    lines = "".join(f"data: {line}\n" for line in payload.split("\n"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}{lines}\n"


# --------------------------------------------------------------------------- #
# 应用装配
# --------------------------------------------------------------------------- #


class Hub:
    """把 registry / orchestrator / dispatcher 打包，避免全局变量散落。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.registry: AgentRegistry = bootstrap(self.settings)
        # 关系图要先建：编排器也要过 delegate 门禁，得先把 guard 造出来
        self.social_graph = self._build_graph()
        self.orchestrator = Orchestrator(
            self.registry, self.registry.bus, guard=self.social_graph
        )
        self.social = SocialHub(self.registry, self.registry.bus, guard=self.social_graph)
        self.dispatcher = JsonRpcDispatcher(
            self.registry, self.orchestrator, self.social, graph=self.social_graph
        )
        #: 自主交友巡航（§6.1 B 辅路径）。**默认关**——`A2A_AUTONOMY_ENABLED`
        #: 为假时 `start()` 是空操作，不会产生任何后台请求。
        self.cruise = SocialCruise(
            self.social_graph,
            enabled=self.settings.autonomy_enabled,
            interval=self.settings.autonomy_interval,
            per_round=self.settings.autonomy_per_round,
            daily=self.settings.autonomy_daily,
        )
        # 社交简报：把「你是谁 / 有哪些好友 / 缺口信号协议」注入 prompt，
        # 让 WorkBuddy / Codex / Claude Code 等 prompt 型 agent 零改动获得
        # 社交感知。社交层关闭时不注入（prompt 逐字节不变）。
        if self.social_graph.enabled and self.settings.social_briefing:
            self.registry.set_briefing_resolver(
                lambda agent_id: social_briefing(self.social_graph, agent_id)
            )

    def _build_graph(self) -> SocialGraph:
        """装配关系图。

        **没有 ``members.yaml`` 时门禁自动关闭**（``enabled=False``），
        于是 ``can()`` 恒真、``visible_contacts()`` 返回 ``None``，
        整个社交层对既有行为完全透明。
        """
        members = SocialGraph.load_members(self.settings.members_path())
        graph = SocialGraph(
            members,
            mode=self.settings.social_mode,
            path=self.settings.relations_path(),
        )
        if graph.enabled:
            # registry 管「agent 存在不存在」，关系图管「能不能交流」。
            # 这里把 registry 里的 agent 补成图节点，缺声明时 owner 留空。
            graph.ensure_agents(
                (rec.id, rec.name) for rec in self.registry.list_records()
            )
            # 能力画像**注入**进去，而不是让关系层 import registry——
            # 分层纪律（约定 1）比少写一个回调重要得多。
            graph.set_capability_resolver(self._capabilities)
            log.info(
                "社交门禁已启用：mode=%s，%d 个成员，%d 条关系",
                graph.mode,
                len(graph.all_members()),
                len(graph._rels),  # noqa: SLF001  只用于启动日志
            )
        else:
            log.info("社交门禁未启用（未找到 %s 或 mode=off）", self.settings.members_file)
        return graph

    # ------------------------------------------------------------------ #
    # 一键启用社交层（免重启）
    # ------------------------------------------------------------------ #

    def ensure_social(self, *, auto_init: bool = True) -> dict[str, Any]:
        """确保社交层可用；未启用时按需生成 ``members.yaml`` 并**热启用**。

        返回 ``{"enabled": bool, "created": bool, "path": str, "members": [...],
        "reason": str}``，可以直接回显给调用方（MCP 工具 / CLI）。

        为什么能「不重启」：:meth:`SocialGraph.reload_members` 是**就地**改成员
        表，所以编排器门禁、SocialHub、dispatcher、巡航、简报 resolver 五个
        持有方拿到的还是同一个对象，下一次访问就看见新状态。

        ``auto_init=False`` 时只做检测，不碰磁盘——留给「我只要看一眼状态」
        的调用方。
        """
        g = self.social_graph
        state: dict[str, Any] = {
            "enabled": bool(g is not None and g.enabled),
            "created": False,
            "path": self.settings.members_file,
            "members": [],
            "reason": "",
        }
        if state["enabled"]:
            state["members"] = [m.id for m in g.all_members()]
            state["reason"] = "社交层已启用"
            return state
        if g is None:
            state["reason"] = "社交图未装配"
            return state
        if self.settings.social_mode == "off":
            state["reason"] = "A2A_SOCIAL_MODE=off，门禁被显式关闭；改成 soft/strict 后重试"
            return state
        if not auto_init:
            state["reason"] = (
                f"社交层未启用（缺 {self.settings.members_file}）。"
                "执行 `social init` 或调用 a2a_social_init 即可一键启用，无需重启。"
            )
            return state

        info = bootstrap_members(
            members_path=self.settings.members_path(),
            agents_path=self.settings.agents_path(),
        )
        state["created"] = bool(info.get("created"))
        state["reason"] = str(info.get("reason") or "")
        if not state["created"] and not info.get("members"):
            # 生成失败（多半是没写权限）——把原因原样带出去，别假装成功
            return state

        members = SocialGraph.load_members(self.settings.members_path())
        if not members:
            state["reason"] = info.get("reason") or "生成的成员表为空，社交层仍关闭"
            return state

        g.reload_members(members)
        g.ensure_agents((rec.id, rec.name) for rec in self.registry.list_records())
        g.set_capability_resolver(self._capabilities)
        if g.enabled and self.settings.social_briefing:
            # 闭包里读的是 self.social_graph，所以这里只在「从未装过」时补一次
            self.registry.set_briefing_resolver(
                lambda agent_id: social_briefing(self.social_graph, agent_id)
            )
        state["enabled"] = bool(g.enabled)
        state["members"] = [m.id for m in g.all_members()]
        if state["enabled"]:
            state["reason"] = (
                f"{info.get('reason') or '已生成成员表'}；社交层已热启用，无需重启"
            )
            log.info("社交门禁已热启用：mode=%s，%d 个成员", g.mode, len(state["members"]))
        return state

    def _capabilities(self, member_id: str) -> dict[str, Any]:
        """给关系层用的能力画像（匹配打分的输入）。

        ``member_id`` 是成员 id；只有 ``agent:*`` 才可能命中 registry。
        人类与外部成员退化为「用 ``bio`` 打分」。
        """
        agent_id = SocialGraph.agent_id_of(member_id)
        if agent_id and self.registry.has(agent_id):
            rec = self.registry.get(agent_id)
            return {
                "name": rec.name,
                "bio": rec.description,
                "description": rec.description,
                "tags": list(rec.spec.tags),
                "skills": [
                    f"{s.name} {s.description} {' '.join(s.tags)}"
                    for s in rec.adapter.skills
                ],
            }
        m = self.social_graph.get_member(member_id) if self.social_graph else None
        return {"bio": m.bio if m else ""}


hub = Hub()


def get_hub() -> Hub:
    return hub


def _bearer(authorization: Optional[str], cookie: Optional[str] = None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return (cookie or "").strip()


async def resolve_member(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Member:
    """解析调用者。**这是唯一的认证入口，所有端点都得经过它。**

    三档行为：

    1. 配了 ``members.yaml`` 且成员配了 token → 按 token 认人，认不出就 403。
    2. 配了 ``members.yaml`` 但谁都没配 token → 本地开发模式，一律按默认成员处理
       （与 v0.3.0「没配 token 就放行」一致，只是身份从匿名变成了 ``human:default``）。
    3. 没配 ``members.yaml`` → 退化成 v0.3.0 的单 token 模式。

    token 来源优先 Authorization 头，其次控制台 Cookie
    （浏览器导航带不了自定义头，见 :data:`CONSOLE_COOKIE`）。
    """
    h = get_hub()
    graph = h.social_graph
    token = _bearer(authorization, request.cookies.get(CONSOLE_COOKIE))

    if graph.enabled:
        if token:
            member = graph.resolve_token(token)
            if member is None:
                raise HTTPException(status_code=403, detail="Token 无效：不属于任何成员")
            return member
        if graph.any_tokens():
            raise HTTPException(status_code=401, detail="缺少 Bearer Token")
        return graph.member_or_synthetic(graph.default_member_id)

    # 无成员表：与 v0.3.0 一致的单 token 行为
    expected = h.settings.api_token
    if expected:
        if not token:
            raise HTTPException(status_code=401, detail="缺少 Bearer Token")
        # 定长比较，避免时序侧信道
        if not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail="Token 无效")
    return Member(id=DEFAULT_MEMBER, name="我", kind="human")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = hub.settings
    if s.require_auth and not s.api_token and not hub.social_graph.any_tokens():
        raise RuntimeError(
            "A2A_REQUIRE_AUTH=true 但既没设 A2A_API_TOKEN，也没有任何成员配 token"
        )
    log.info("A2A Hub 启动：%s:%s（%d 个 agent）", s.host, s.port, len(hub.registry.list_records()))

    # 两个「静默降级」陷阱，宁可在启动日志里唠叨一句：
    # 1) 多人类场景下没声明 owner 的 agent 拿不到执行类权限，谁都使唤不动
    # 2) 声明了成员但一个 token 都没配 —— 门禁形同虚设
    graph = hub.social_graph
    if graph.enabled:
        orphan = graph.warn_undeclared_owners()
        if orphan:
            log.warning(
                "以下 agent 未声明 owner，将无法获得执行类权限（指派不动）：%s",
                "、".join(orphan),
            )
        if not graph.any_tokens():
            log.warning(
                "members.yaml 里没有任何成员配置 token —— 门禁会启用，"
                "但请求一律按 `%s` 处理。要真正区分调用者请给成员配 tokens。",
                graph.default_member_id,
            )
        # 启动时把「过期租约」清一遍：stale 边的执行类权限降级回对话类（§7.4）。
        decayed = graph.sweep_stale()
        if decayed:
            log.info("信任衰减：%d 条长期未互动的边降级了执行类权限", len(decayed))

    # 启动时做一次非阻塞健康探测，让控制台一打开就有状态
    asyncio.create_task(hub.registry.check_health(force=True))
    # 巡航只有显式开启才会真的跑（默认关 = 零后台请求）
    if hub.cruise.start():
        log.info("自主交友巡航已启用（A2A_AUTONOMY_ENABLED=true）")
    yield
    await hub.cruise.stop()
    await hub.social.aclose()
    await hub.registry.aclose()


app = FastAPI(
    title="A2A Hub",
    description="异构 AI Agent 互联互通与多智能体协同网关",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["基础"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "hub": hub.settings.hub_name,
        "agents": len(hub.registry.list_records()),
        "version": "0.2.0",
    }


@app.get("/", include_in_schema=False)
async def root_redirect() -> Any:
    # 浏览器访问根路径时给个引导页，机器访问走 POST /（JSON-RPC）
    return JSONResponse(
        {
            "name": hub.settings.hub_name,
            "protocol": "A2A v0.3.0 (JSON-RPC 2.0 over HTTP)",
            "agentCard": f"{hub.settings.public_url.rstrip('/')}/.well-known/agent.json",
            "console": f"{hub.settings.public_url.rstrip('/')}/console",
            "endpoints": {
                "jsonrpc": "POST /",
                "agentScopedRpc": "POST /agents/{agentId}/",
                "agents": "GET /agents",
                "tasks": "GET /tasks",
                "sse": "GET /tasks/{taskId}/events",
                "collaboration": "POST /collab",
            },
            "methods": JsonRpcDispatcher.supported_methods(),
        }
    )


# --------------------------------------------------------------------------- #
# 发现层
# --------------------------------------------------------------------------- #


@app.get("/.well-known/agent.json", tags=["发现"])
@app.get("/.well-known/agent-card.json", tags=["发现"], include_in_schema=False)
async def hub_agent_card() -> dict[str, Any]:
    return hub.registry.hub_card(social=hub.social_graph)


@app.get("/capabilities", tags=["发现"])
async def capabilities(base_url: str = Query(default="")) -> dict[str, Any]:
    """本 Hub 的对外能力清单（机器可读）。

    与 CLI ``run.py capabilities``、JSON-RPC ``hub/capabilities`` **同源**。

    刻意**不做鉴权**：新 agent 接进来时要先看到「有什么能力、怎么调」，
    若连说明书都要 token，就会陷入「要先有 token 才知道怎么拿 token」。
    清单里只有能力名称与调用方式，不含密钥，也不含成员隐私。
    """
    return manifest(base_url=base_url)


@app.get("/agents", tags=["发现"])
async def list_agents(refresh: bool = Query(default=False), auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    if refresh:
        await hub.registry.check_health(force=True)
    return {"count": len(hub.registry.list_records()), "agents": hub.registry.snapshot()}


@app.get("/agents/{agent_id}/.well-known/agent.json", tags=["发现"])
@app.get("/agents/{agent_id}/.well-known/agent-card.json", tags=["发现"], include_in_schema=False)
async def agent_card(agent_id: str) -> dict[str, Any]:
    try:
        card = hub.registry.agent_card(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 社交声明在这里补，而不是塞进适配器 —— 适配器不该知道社交层的存在
    if hub.social_graph.enabled:
        card["extensions"] = [
            *card.get("extensions", []),
            {
                "uri": "https://a2a-hub.local/x-social",
                "required": False,
                "description": "本 agent 受好友制访问控制保护，非好友调用返回 -32008。",
            },
        ]
    return card


@app.get("/agents/{agent_id}", tags=["发现"])
async def agent_detail(agent_id: str, auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    try:
        rec = hub.registry.get(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return rec.snapshot(hub.settings.public_url)


@app.post("/agents/{agent_id}/", tags=["A2A"])
async def agent_scoped_rpc(
    agent_id: str, request: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> Any:
    return await _handle_rpc(request, scoped_agent=agent_id, actor=auth.id)


# --------------------------------------------------------------------------- #
# A2A JSON-RPC 入口
# --------------------------------------------------------------------------- #


@app.post("/", tags=["A2A"])
async def jsonrpc_entry(
    request: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> Any:
    return await _handle_rpc(request, scoped_agent=None, actor=auth.id)


async def _handle_rpc(
    request: dict[str, Any], scoped_agent: Optional[str], actor: str = USER
) -> Any:
    """统一入口。method 需要流式时返回 SSE，否则返回 JSON。

    ``actor`` 来自鉴权结果，由这里传给分发器——**JSON-RPC 层不解析身份**，
    否则「谁能自称是谁」就会变成每个方法各自的实现细节。
    """
    method = request.get("method")

    if method == "message/stream":
        return await _stream_response(
            hub.dispatcher.handle(request, scoped_agent, actor),
            method_name="message/stream",
        )
    if method == "tasks/resubscribe":
        return await _stream_response(
            hub.dispatcher.handle(request, scoped_agent, actor),
            method_name="tasks/resubscribe",
        )

    result = await hub.dispatcher.handle(request, scoped_agent, actor)

    # message/stream 之外的流式方法（dispatch 返回了异步生成器）兜底
    if hasattr(result, "__aiter__"):
        return await _stream_from_generator(result)
    return JSONResponse(result)


async def _stream_response(rpc_coro: Any, method_name: str) -> StreamingResponse:
    """把一个 dispatch 结果包装成 SSE 流。"""
    result = await rpc_coro
    if not hasattr(result, "__aiter__"):
        # 出错了（返回了 JSON-RPC error 响应）
        async def single() -> AsyncIterator[str]:
            yield sse_format(result, event="error")
            yield sse_format({"kind": "done"}, event="done")

        return StreamingResponse(single(), media_type="text/event-stream", headers=SSE_HEADERS)

    return await _stream_from_generator(result)


async def _stream_from_generator(gen: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        try:
            async for event in gen:
                kind = event.get("kind", "event")
                yield sse_format(event, event=kind)
                if kind == "status-update" and event.get("final"):
                    break
        except asyncio.CancelledError:
            raise
        except A2AError as exc:
            # A2A 域内的错误必须**保住自己的错误码**。否则社交门禁的 -32008
            # 会在流式路径上被压成一个泛泛的 -32603，客户端就分不清
            # 「我该去加好友」和「Hub 炸了」。注意异常是迭代时才抛的，
            # 所以这段捕获不是摆设。
            yield sse_format(exc.to_rpc_error().model_dump(exclude_none=True), event="error")
        except Exception as exc:  # noqa: BLE001
            yield sse_format(
                JsonRpcError(
                    code=JsonRpcErrorCodes.INTERNAL_ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                ).model_dump(),
                event="error",
            )
        finally:
            yield sse_format({"kind": "done"}, event="done")

    return StreamingResponse(
        event_source(), media_type="text/event-stream", headers=SSE_HEADERS
    )


# --------------------------------------------------------------------------- #
# 任务层
# --------------------------------------------------------------------------- #


@app.get("/tasks", tags=["任务"])
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=500), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    tasks = hub.registry.store.list(limit)
    return {
        "count": len(tasks),
        "tasks": [_task_brief(t) for t in tasks],
    }


def _task_brief(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "agentId": task.agentId,
        "contextId": task.contextId,
        "state": task.status.state.value,
        "createdAt": task.createdAt,
        "updatedAt": task.updatedAt,
        "prompt": (task.history[0].text()[:200] if task.history else ""),
        "resultPreview": (task.final_text() or "")[:300],
    }


@app.get("/tasks/{task_id}", tags=["任务"])
async def get_task(
    task_id: str, historyLength: Optional[int] = Query(default=None),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    task = hub.registry.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未找到任务 {task_id}")
    # REST 视图带上 Hub 内部字段（agentId / createdAt / updatedAt）；
    # A2A 线上载荷不带这些，见 store.task_to_wire
    data = task_to_wire(task, include_internal=True)
    if historyLength is not None:
        data["history"] = data["history"][-historyLength:] if historyLength else []
    return data


@app.post("/tasks/{task_id}/cancel", tags=["任务"])
async def cancel_task(task_id: str, auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    try:
        task = await hub.registry.cancel(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task.model_dump(mode="json")


@app.get("/tasks/{task_id}/events", tags=["任务"])
async def task_events(request: Request, task_id: str) -> StreamingResponse:
    task = hub.registry.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未找到任务 {task_id}")

    async def source() -> AsyncIterator[str]:
        bus = hub.registry.bus
        q = bus.subscribe(task_id)
        # 同样避免在 finally 里 yield（断连时会 RuntimeError）
        try:
            yield sse_format(task.model_dump(mode="json"), event="task")
            if not task.status.is_terminal:
                while True:
                    if await request.is_disconnected():
                        return
                    got, event = await bus.next_event(q, timeout=25.0)
                    if not got:
                        # 空闲心跳；同时兜底检查任务是否已终态，避免流悬挂
                        current = hub.registry.store.get(task_id)
                        if current is not None and current.status.is_terminal:
                            yield sse_format(current.model_dump(mode="json"), event="task")
                            break
                        yield ": keepalive\n\n"
                        continue
                    if event is STREAM_END:
                        break
                    payload = (
                        event.model_dump(mode="json") if hasattr(event, "model_dump") else event
                    )
                    kind = payload.get("kind", "event") if isinstance(payload, dict) else "event"
                    yield sse_format(payload, event=kind)
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(task_id, q)
        yield sse_format({"kind": "done"}, event="done")

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/events", tags=["任务"])
async def global_events(request: Request) -> StreamingResponse:
    """全局事件流：控制台用它实时刷新所有 agent 的活动。"""

    async def source() -> AsyncIterator[str]:
        bus = hub.registry.bus
        q = bus.subscribe_global()
        try:
            while True:
                if await request.is_disconnected():
                    break
                got, event = await bus.next_event(q, timeout=25.0)
                if not got:
                    yield ": keepalive\n\n"
                    continue
                if event is STREAM_END:
                    break
                yield sse_format(event, event="hub")
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe_global(q)

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- #
# 协同层
# --------------------------------------------------------------------------- #


@app.post("/collab", tags=["协同"])
async def create_collab(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    mode = payload.get("mode") or "broadcast"
    prompt = payload.get("prompt")
    if not prompt:
        raise HTTPException(status_code=422, detail="缺少 prompt")

    agent_ids = [str(a) for a in (payload.get("agentIds") or [])]
    # **二道门**：协同是「让 agent 干活」，必须在入口就过 delegate。
    # 自动路由那部分由编排器自己过滤（它拿着同一个 guard）。
    g = hub.social_graph
    refused = g.refused_agents(auth.id, agent_ids) if g.enabled else []
    if refused:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "missing_scope",
                "peers": refused,
                "needScope": "delegate",
                "hint": "请对方（或其 owner）授予你 `delegate`：PATCH /social/relations/<你>",
                "message": "以下 agent 未授予你执行权（delegate）：" + "、".join(refused),
            },
        )

    try:
        run = hub.orchestrator.create_run(
            mode, prompt, agent_ids, payload.get("options") or {}, actor=auth.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        if payload.get("blocking", False):
            await hub.orchestrator.run_sync(run)
        else:
            await hub.orchestrator.launch(run)
    except SocialError as exc:
        raise HTTPException(status_code=getattr(exc, "status", 403), detail=str(exc)) from exc
    return run.model_dump(mode="json")


@app.get("/collab", tags=["协同"])
async def list_collab(
    limit: int = Query(default=30, ge=1, le=200), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    runs = hub.orchestrator.list_runs(limit)
    return {"count": len(runs), "runs": [r.model_dump(mode="json") for r in runs]}


@app.get("/collab/{run_id}", tags=["协同"])
async def get_collab(run_id: str, auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    run = hub.orchestrator.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未找到协同运行 {run_id}")
    return run.model_dump(mode="json")


@app.delete("/collab/{run_id}", tags=["协同"])
async def cancel_collab(run_id: str, auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    ok = await hub.orchestrator.cancel_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到协同运行 {run_id}")
    return {"canceled": True, "runId": run_id}


@app.get("/collab/{run_id}/events", tags=["协同"])
async def collab_events(request: Request, run_id: str) -> StreamingResponse:
    if hub.orchestrator.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"未找到协同运行 {run_id}")

    async def source() -> AsyncIterator[str]:
        try:
            async for event in hub.orchestrator.stream(run_id):
                if await request.is_disconnected():
                    break
                yield sse_format(event, event="collab")
        except asyncio.CancelledError:
            raise
        finally:
            yield sse_format({"kind": "done"}, event="done")

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- #
# 会话层（IM）—— 把 agent 当"好友"聊
# --------------------------------------------------------------------------- #
#
# 这一节里**所有**端点都挂 resolve_member，而且 sender / reader / actor
# 一律从鉴权结果取，请求体里的同名字段直接忽略——
# 「校验客户端自报的身份」很容易被后续改动绕过去，「忽略」不会。


def _view(conv_id: str, member: Member) -> Any:
    """取会话并做可见性检查。"""
    try:
        conv = hub.social.get(conv_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not hub.social.can_view(conv, member.id):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return conv


@app.get("/im/contacts", tags=["会话"])
async def im_contacts(
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """通讯录：可聊的 agent 列表 + 在线状态 + 我对它有哪些权限。

    ``?as=<自己的 agent>`` 可以看「那个 agent 能联系到谁」。
    """
    who = _viewer(auth, as_member)
    items = hub.social.contacts(who)
    return {"count": len(items), "contacts": items, "me": who}


@app.get("/im/conversations", tags=["会话"])
async def im_list_conversations(auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    """会话列表（带最后一条消息与未读数）——相当于微信首页。

    只返回**我的**会话，未读也是按我算的。
    """
    items = hub.social.list_conversations(auth.id)
    return {"count": len(items), "conversations": items}


@app.post("/im/conversations", tags=["会话"])
async def im_create_conversation(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """新建会话。

    单聊：``{"kind": "direct", "agent": "echo"}``（幂等，重复调用返回同一个会话）
    群聊：``{"kind": "group", "members": ["echo","static"], "title": "架构组"}``
    """
    kind = str(payload.get("kind") or "direct").lower()
    try:
        if kind == "direct":
            agent_id = payload.get("agent") or payload.get("agentId")
            if not agent_id:
                raise HTTPException(status_code=422, detail="单聊需要 `agent` 参数")
            conv = hub.social.open_direct(str(agent_id), owner=auth.id)
        elif kind == "group":
            members = payload.get("members") or payload.get("agentIds") or []
            if not members:
                raise HTTPException(status_code=422, detail="群聊需要 `members` 参数")
            conv = hub.social.create_group(
                [str(m) for m in members],
                str(payload.get("title") or ""),
                bool(payload.get("autoRoute", True)),
                actor=auth.id,
            )
        else:
            raise HTTPException(status_code=422, detail=f"未知会话类型 `{kind}`")
    except A2AError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return hub.social.summary(conv, auth.id)


@app.get("/im/conversations/{conv_id}", tags=["会话"])
async def im_get_conversation(
    conv_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """拉取聊天记录（含每条消息的投递回执）。"""
    conv = _view(conv_id, auth)
    return hub.social.history(conv.id, limit=limit, viewer=auth.id)


@app.post("/im/conversations/{conv_id}/messages", tags=["会话"])
async def im_send_message(
    conv_id: str,
    payload: dict[str, Any] = Body(...),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """发消息。

    **立即返回**：消息先落库并广播，被唤醒的 agent 在后台跑完再回话——
    要拿到回复请订阅 ``GET /im/conversations/{id}/events``。

    发送者取自鉴权结果，body 里的 ``sender`` 被忽略。
    """
    conv = _view(conv_id, auth)
    text = payload.get("text") or payload.get("content") or ""
    try:
        return await hub.social.send(
            conv.id,
            str(text),
            sender=auth.id,
            reply_to=payload.get("replyTo"),
            wake=payload.get("wake"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/im/conversations/{conv_id}/read", tags=["会话"])
async def im_mark_read(
    conv_id: str,
    payload: Optional[dict[str, Any]] = Body(default=None),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """清空未读红点。

    **``reader`` 一律取鉴权结果**：旧实现从请求体读，等于谁都能替别人清未读，
    回执状态直接失真。body 里的同名字段现在被忽略。
    """
    del payload  # 显式忽略：这是刻意的，不是漏了
    conv = _view(conv_id, auth)
    hub.social.mark_read(conv.id, auth.id)
    return hub.social.summary(conv, auth.id)


@app.patch("/im/conversations/{conv_id}", tags=["会话"])
async def im_update_conversation(
    conv_id: str,
    payload: dict[str, Any] = Body(...),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """群聊拉人 / 踢人：``{"add": ["echo"], "remove": ["static"]}``。"""
    conv = _view(conv_id, auth)
    try:
        conv = hub.social.update_group(
            conv.id, payload.get("add"), payload.get("remove"), actor=auth.id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, A2AError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return hub.social.summary(conv, auth.id)


@app.delete("/im/conversations/{conv_id}", tags=["会话"])
async def im_disband(conv_id: str, auth: Member = Depends(resolve_member)) -> dict[str, Any]:
    _view(conv_id, auth)
    try:
        gone = hub.social.disband(conv_id, actor=auth.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not gone:
        raise HTTPException(status_code=404, detail=f"未找到会话 {conv_id}")
    return {"disbanded": True, "conversationId": conv_id}


@app.get("/im/conversations/{conv_id}/events", tags=["会话"])
async def im_events(
    request: Request, conv_id: str, auth: Member = Depends(resolve_member)
) -> StreamingResponse:
    """会话实时事件流：新消息 / 回执变更 / 心跳。

    **这里以前完全没挂鉴权**，是这个项目最严重的一个现存缺口：
    它绕过 ``A2A_API_TOKEN``，任何人只要猜到（或看到）会话 id
    就能订阅到全部对话内容。现在补上了认证 + 可见性检查。
    """
    _view(conv_id, auth)

    async def source() -> AsyncIterator[str]:
        try:
            async for event in hub.social.stream(conv_id):
                if await request.is_disconnected():
                    return
                yield sse_format(event, event="im")
        except asyncio.CancelledError:
            raise
        # 正常结束不额外 yield（避免在 finally 里产出）
        return

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- #
# 社交层（/social/*）—— 成员、好友关系与交流权限
# --------------------------------------------------------------------------- #


def _graph() -> SocialGraph:
    return hub.social_graph


def _to_http(exc: SocialError) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))


def _need_graph() -> SocialGraph:
    """门禁没启用时，社交端点整体不可用。

    不返回空列表而是直接 409：静默返回空集会让调用方以为「世界上没有成员」，
    而事实是「你根本没配 members.yaml」——这两件事的排查成本差着量级。
    """
    g = _graph()
    if not g.enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "社交层未启用：未找到 members.yaml，或 A2A_SOCIAL_MODE=off。"
                f"当前配置路径 {hub.settings.members_file}"
            ),
        )
    return g


def _viewer(auth: Member, as_member: Optional[str]) -> str:
    """只读接口的「以谁的身份看」。

    owner 可以带 ``?as=<自己的 agent>`` 去看 agent 的视角——
    否则 agent 收到的好友申请就没人能处理了（agent 自己不会点「同意」）。
    **只能代表自己的 agent**，越权直接 403；这条规则与写入侧的
    ``accept/reject(as=...)`` 共用 :meth:`SocialGraph.can_act_for`。
    """
    if not as_member:
        # 不带 `as` 就是看自己，**不需要门禁启用**——控制台/CLI 在无
        # members.yaml 时依然要能拿 `/social/me` 与 `/im/contacts`。
        return auth.id
    g = _need_graph()
    target = g.normalize(as_member)
    if target != auth.id and not g.can_act_for(auth.id, target):
        raise HTTPException(status_code=403, detail=f"无权以 `{target}` 的身份查看")
    return target

def _publish_social(event: str, actor: str, **payload: Any) -> None:
    """关系变化也发事件——审计与事件同源，不写两套。"""
    hub.registry.bus.publish(
        "social",
        {
            "kind": "social-event",
            "event": event,
            "actor": actor,
            "ts": utc_now(),
            **payload,
        },
    )


@app.get("/social/me", tags=["社交"])
async def social_me(
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """我的名片 + 我的关系 + 待办数。CLI 与控制台都靠它拿身份。

    owner 可带 ``?as=<自己的 agent>`` 看它的视角（如查它收到的申请）。
    """
    return _graph().me(_viewer(auth, as_member))


@app.get("/social/members", tags=["社交"])
async def social_members(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """搜索可发现成员。

    **按调用者的三档可见性过滤**：``private``（默认）谁都搜不到、
    ``circle`` 限度 ≤ 2、``public`` 人人可见；被共同好友引荐过的例外。
    私有只挡「被发现」，不挡「被指名申请」。
    """
    g = _need_graph()
    who = _viewer(auth, as_member)
    items = g.search(q, viewer=who, limit=limit)
    return {"count": len(items), "members": items, "query": q, "me": who}


@app.get("/social/members/{member_id}", tags=["社交"])
async def social_member_profile(
    member_id: str,
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """看别人的名片。

    **只给共同好友「数」，不给名单。** 名单一旦可读，加一个人就等于
    交出整个通讯录，再扩散一轮就拿到了全图——这是社交网络最经典的隐私事故。
    """
    g = _need_graph()
    data = g.profile(member_id, auth.id)
    if data["distance"] == 0 and data["member"]["id"] != auth.id:
        raise HTTPException(status_code=404, detail=f"未找到成员 {member_id}")
    return data


@app.get("/social/discover", tags=["社交"])
async def social_discover(
    need: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """自主发现：按匹配度推荐值得认识的对象，附各维度打分明细。

    这就是「扩大圈层」的入口。``need`` 是目前的缺口（如「OCR 表格提取」），
    给了它才能算「技能互补」；不给则退化为按共同好友与同类度排序。
    """
    g = _need_graph()
    who = _viewer(auth, as_member)
    items = g.discover(who, need=need, limit=limit)
    return {"count": len(items), "candidates": items, "need": need, "me": who}


@app.get("/social/introductions", tags=["社交"])
async def social_introductions(
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """别人引荐给我的人（收件箱式）。**引荐不自动授予任何权限。**"""
    g = _need_graph()
    who = _viewer(auth, as_member)
    items = g.introductions(who)
    return {"count": len(items), "introductions": items, "me": who}


@app.post("/social/introductions", tags=["社交"])
async def social_introduce(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """引荐：``{"peer": "codex", "to": "guest", "note": "他做过类似的事"}``。

    只有**同时是双方好友**的人才能引荐。引荐只提高可信度并让 ``private``
    成员可被对方发现，**不授予任何 scope**——权限仍然要接收方自己给。
    """
    g = _need_graph()
    peer = payload.get("peer")
    to = payload.get("to") or payload.get("target")
    if not peer or not to:
        raise HTTPException(status_code=422, detail="需要 `peer` 与 `to` 参数")
    try:
        result = g.introduce(
            auth.id, str(to), str(peer), str(payload.get("note") or "")
        )
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social(
        "introduce",
        auth.id,
        peer=g.normalize(str(peer)),
        peers=[g.normalize(str(peer)), g.normalize(str(to))],
    )
    return result


@app.get("/social/pending", tags=["社交"])
async def social_pending(
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """需要我拍板的自主交友待办（§6 约束 2「越界必须人审」）。

    待办分两种：``kind=accept``（别人申请我，自动策略觉得该人批）、
    ``kind=request``（我自己的 agent 想申请某人，但策略要求人批）。
    每条都带 ``policy`` 与 ``detail``——回答「为什么轮到我拍板」，
    这是自主同意可解释性的落点（§6 约束 3）。
    """
    g = _need_graph()
    who = _viewer(auth, as_member)
    items = g.pending_approvals(owner=who)
    return {
        "count": len(items),
        "pending": items,
        "me": who,
        "autonomyEnabled": bool(hub.settings.autonomy_enabled),
    }


@app.post("/social/pending/approve", tags=["社交"])
async def social_pending_approve(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """批准一条待办：``{"id": "ap-xxx", "scopes": ["peek","chat"]}``。

    **这是「越界自主行为成真」的唯一路径**，所以它必须由人来做。
    ``scopes`` 不传就用待办里记录的请求范围。
    """
    g = _need_graph()
    pid = payload.get("id") or payload.get("approval")
    if not pid:
        raise HTTPException(status_code=422, detail="需要 `id` 参数")
    who = auth.id
    entry = g.get_approval(str(pid))
    if entry is None:
        raise HTTPException(status_code=404, detail="待办不存在或已处理")
    if entry.get("owner") != who and not g.can_act_for(who, entry.get("owner", "")):
        raise HTTPException(status_code=403, detail="只有该待办的归属人才能批准")
    try:
        result = g.approve_pending(who, str(pid), payload.get("scopes"))
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social(
        "pending-approved",
        who,
        peer=entry.get("requester"),
        approval=result["approval"],
    )
    return result


@app.post("/social/pending/deny", tags=["社交"])
async def social_pending_deny(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """驳回一条待办：``{"id": "ap-xxx", "reason": "标签不符"}``。理由只进自己的审计。"""
    g = _need_graph()
    pid = payload.get("id") or payload.get("approval")
    if not pid:
        raise HTTPException(status_code=422, detail="需要 `id` 参数")
    who = auth.id
    entry = g.get_approval(str(pid))
    if entry is None:
        raise HTTPException(status_code=404, detail="待办不存在或已处理")
    if entry.get("owner") != who and not g.can_act_for(who, entry.get("owner", "")):
        raise HTTPException(status_code=403, detail="只有该待办的归属人才能驳回")
    try:
        result = g.deny_pending(who, str(pid), str(payload.get("reason") or ""))
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("pending-denied", who, peer=entry.get("requester"))
    return result


@app.post("/social/need", tags=["社交"])
async def social_need(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """报告一个能力缺口（§6.1 A 主路径）。

    ``{"need": "pdf-extract", "reason": "扫描件表格提取", "as": "codex"}``
    —— agent 干活时发现自己干不了，就把信号交给 Hub：由它去 ``discover``，
    再按策略决定「发申请」「挂 owner 待办」还是「什么都不做」。
    **申请只请求对话类权限**，执行类永远要人来授权。
    """
    g = _need_graph()
    need = str(payload.get("need") or "").strip()
    if not need:
        raise HTTPException(status_code=422, detail="需要 `need` 参数")
    as_member = payload.get("as")
    who = _viewer(auth, as_member) if as_member else auth.id
    result = g.request_for_need(who, need, reason=str(payload.get("reason") or ""))
    if result.get("asked"):
        _publish_social("need", who, peer=result["asked"].get("peer"), need=need)
    return result


@app.get("/social/relations", tags=["社交"])
async def social_relations(
    state: Optional[str] = Query(default=None),
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """我的关系列表。

    **只能看自己的边**——好友列表一旦能被非好友读到，加一个人就等于
    交出整个通讯录，再扩散一轮就拿到了全图。``?as=`` 只放宽到自己的 agent。
    """
    states = None
    if state:
        try:
            states = {RelationState(state)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"未知状态 `{state}`") from exc
    items = _graph().relations_of(_viewer(auth, as_member), states=states)
    return {"count": len(items), "relations": items}


@app.get("/social/requests", tags=["社交"])
async def social_requests(
    box: str = Query(default="in", pattern="^(in|out)$"),
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """``box=in`` 收件箱（待我处理），``box=out`` 我发出的在途申请。

    owner 用 ``?as=<自己的 agent>`` 就能看到 agent 收到的申请并代为处理。
    """
    g = _graph()
    who = _viewer(auth, as_member)
    items = g.inbox(who) if box == "in" else g.outbox(who)
    return {"count": len(items), "box": box, "member": who, "requests": items}


@app.post("/social/requests", tags=["社交"])
async def social_request(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """发好友申请：``{"to": "codex", "message": "想用你的算法能力", "scopes": ["chat"]}``。

    理由必填——既是防骚扰，也让对方知道你是谁。
    """
    g = _need_graph()
    to = payload.get("to") or payload.get("peer")
    if not to:
        raise HTTPException(status_code=422, detail="需要 `to` 参数")
    try:
        rel = g.request(
            auth.id,
            str(to),
            str(payload.get("message") or ""),
            payload.get("scopes"),
        )
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("request", auth.id, peer=g.normalize(str(to)), peers=[g.normalize(str(to))])
    return g.describe(rel, auth.id)


@app.post("/social/requests/accept", tags=["社交"])
async def social_accept(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """同意申请：``{"peer": "guest", "scopes": ["peek","chat"], "as": "codex"}``。

    ``scopes`` 是**我实际授予对方**的范围，默认只给对话类；
    ``delegate`` 必须显式写出来。``as`` 让 owner 代表自己的 agent 同意。
    """
    g = _need_graph()
    peer = payload.get("peer") or payload.get("from") or payload.get("to")
    if not peer:
        raise HTTPException(status_code=422, detail="需要 `peer` 参数")
    try:
        rel = g.accept(
            auth.id,
            str(peer),
            payload.get("scopes"),
            as_member=payload.get("as"),
        )
    except SocialError as exc:
        raise _to_http(exc) from exc
    # 重新成为好友 → 解冻历史单聊。少了这步：open_direct 幂等会返回那个
    # 已归档的会话，于是「加回来了却发不出消息」，极难自查。
    acting = g.normalize(payload.get("as") or auth.id)
    hub.social.thaw_between(acting, g.normalize(str(peer)))
    _publish_social("accept", auth.id, peer=g.normalize(str(peer)), peers=[g.normalize(str(peer))])
    return g.describe(rel, auth.id)


@app.post("/social/requests/reject", tags=["社交"])
async def social_reject(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """拒绝申请。``reason`` 只写进自己的审计，**不告诉对方**。"""
    g = _need_graph()
    peer = payload.get("peer") or payload.get("from")
    if not peer:
        raise HTTPException(status_code=422, detail="需要 `peer` 参数")
    try:
        rel = g.reject(
            auth.id, str(peer), str(payload.get("reason") or ""), as_member=payload.get("as")
        )
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("reject", auth.id, peer=g.normalize(str(peer)))
    return g.describe(rel, auth.id)


@app.post("/social/requests/cancel", tags=["社交"])
async def social_cancel(
    payload: dict[str, Any] = Body(...), auth: Member = Depends(resolve_member)
) -> dict[str, Any]:
    """撤回自己发出的申请。"""
    g = _need_graph()
    peer = payload.get("peer") or payload.get("to")
    if not peer:
        raise HTTPException(status_code=422, detail="需要 `peer` 参数")
    try:
        rel = g.cancel(auth.id, str(peer))
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("cancel", auth.id, peer=g.normalize(str(peer)))
    return g.describe(rel, auth.id)


@app.patch("/social/relations/{peer}", tags=["社交"])
async def social_set_grant(
    peer: str,
    payload: dict[str, Any] = Body(...),
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """改权限：``{"scopes": ["peek","chat","delegate"]}``。

    会强制检查**权限上行闭包**——不能授予自己没有的东西。

    ``?as=`` 让 owner 代表自己的 agent 授权。**``delegate`` 靠这条才授得出去**：
    agent 自己不会调 CLI，owner 若不代劳，执行权永远开不了。
    """
    g = _need_graph()
    who = _viewer(auth, as_member)
    try:
        rel = g.set_grant(auth.id, peer, payload.get("scopes") or [], as_member=who)
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("grant", who, peer=g.normalize(peer), peers=[g.normalize(peer)])
    return g.describe(rel, who)


@app.delete("/social/relations/{peer}", tags=["社交"])
async def social_revoke(
    peer: str,
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """删好友。保留历史归档，只禁止新消息。``?as=`` 可代表自己的 agent。"""
    g = _need_graph()
    who = _viewer(auth, as_member)
    try:
        rel = g.revoke(auth.id, peer, as_member=who)
    except SocialError as exc:
        raise _to_http(exc) from exc
    # 关系解除 → 已有单聊归档（可读不可写）。否则「解除了却还能继续说话」
    # 等于关系根本没解除。
    hub.social.freeze_between(who, g.normalize(peer))
    _publish_social("revoke", who, peer=g.normalize(peer), peers=[g.normalize(peer)])
    return g.describe(rel, who)


@app.post("/social/relations/{peer}/block", tags=["社交"])
async def social_block(
    peer: str,
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """拉黑。单向，且只有你能解除。对方向你发申请会被静默丢弃。"""
    g = _need_graph()
    who = _viewer(auth, as_member)
    try:
        rel = g.block(auth.id, peer, as_member=who)
    except SocialError as exc:
        raise _to_http(exc) from exc
    hub.social.freeze_between(who, g.normalize(peer))
    _publish_social("block", who, peer=g.normalize(peer))
    return g.describe(rel, who)


@app.delete("/social/relations/{peer}/block", tags=["社交"])
async def social_unblock(
    peer: str,
    as_member: Optional[str] = Query(default=None, alias="as"),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """解除拉黑。"""
    g = _need_graph()
    who = _viewer(auth, as_member)
    try:
        rel = g.unblock(auth.id, peer, as_member=who)
    except SocialError as exc:
        raise _to_http(exc) from exc
    _publish_social("unblock", who, peer=g.normalize(peer))
    return g.describe(rel, who)


@app.get("/social/audit", tags=["社交"])
async def social_audit(
    peer: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    auth: Member = Depends(resolve_member),
) -> dict[str, Any]:
    """审计链：谁在什么时候把什么权限给了谁。

    **只返回与本人（或本人代理的 agent）相关的记录**——审计是全 Hub 共享的，
    不过滤就等于把所有人的社交往来摊开给任何一个成员。
    """
    who = auth.id
    g = _need_graph()
    mine = {who}
    for m in g.all_members():
        if m.kind == "agent" and g.can_act_for(who, m.id):
            mine.add(m.id)
    items = [
        a for a in g.trail(peer, limit=limit)
        if a.get("actor") in mine or a.get("peer") in mine
    ]
    return {"count": len(items), "audit": items}


@app.get("/social/events", tags=["社交"])
async def social_events(
    request: Request, auth: Member = Depends(resolve_member)
) -> StreamingResponse:
    """社交事件流：申请 / 同意 / 拒绝 / 权限变更。只推送与本人相关的事件。"""
    _need_graph()

    async def source() -> AsyncIterator[str]:
        bus = hub.registry.bus
        channel = "social"
        q = bus.subscribe(channel)
        try:
            # 先订阅再发快照（项目约定），否则快事件会掉进订阅窗口里被丢掉
            yield sse_format(
                {
                    "kind": "social-event",
                    "event": "snapshot",
                    "me": _graph().me(auth.id),
                },
                event="social",
            )
            while True:
                got, item = await bus.next_event(q, 25.0)
                if await request.is_disconnected():
                    return
                if not got:
                    yield sse_format(
                        {"kind": "social-event", "event": "heartbeat"}, event="social"
                    )
                    continue
                if item.get("actor") == auth.id or auth.id in (item.get("peers") or []):
                    yield sse_format(item, event="social")
        finally:
            bus.unsubscribe(channel, q)

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- #
# Web 控制台
# --------------------------------------------------------------------------- #


@app.get("/console", include_in_schema=False)
@app.get("/console/", include_in_schema=False)
async def console(
    request: Request,
    token: Optional[str] = Query(default=None, include_in_schema=False),
    authorization: Optional[str] = Header(default=None),
) -> Any:
    """控制台入口。

    浏览器导航时**带不了自定义请求头**，所以 token 支持三种来源：
    ``?token=`` 查询参数、``Authorization`` 头、以及登录后写下的
    :data:`CONSOLE_COOKIE`。查询参数只在首次进入时用一次，
    随后立刻转成 HttpOnly Cookie——否则 token 会留在浏览器历史和访问日志里。
    """
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="控制台静态文件缺失")

    graph = _graph()
    presented = _bearer(authorization, request.cookies.get(CONSOLE_COOKIE)) or (token or "")
    if token:
        presented = token

    ok = True
    if graph.enabled:
        if presented:
            ok = bool(graph.resolve_token(presented))
        else:
            ok = not graph.any_tokens()
    elif hub.settings.api_token:
        ok = bool(presented) and hmac.compare_digest(presented, hub.settings.api_token)

    if not ok:
        raise HTTPException(
            status_code=401,
            detail="控制台需要 Token：请在 URL 后加 ?token=<你的 token> 再访问",
        )

    resp = FileResponse(index)
    if token:
        resp.set_cookie(
            CONSOLE_COOKIE, token, httponly=True, samesite="lax", path="/"
        )
    return resp


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
