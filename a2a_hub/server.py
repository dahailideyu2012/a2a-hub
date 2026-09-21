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

控制台
  GET  /console                                Web 控制台
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .models import A2AError, JsonRpcError, JsonRpcErrorCodes, JsonRpcResponse
from .orchestrator import Orchestrator
from .registry import AgentRegistry, bootstrap
from .rpc import JsonRpcDispatcher
from .social import SocialHub, USER
from .store import task_to_wire

log = logging.getLogger("a2a_hub.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

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
        self.orchestrator = Orchestrator(self.registry, self.registry.bus)
        self.social = SocialHub(self.registry, self.registry.bus)
        self.dispatcher = JsonRpcDispatcher(self.registry, self.orchestrator, self.social)


hub = Hub()


def get_hub() -> Hub:
    return hub


async def require_auth(
    authorization: Optional[str] = Header(default=None),
) -> None:
    """可选的 Bearer 鉴权。未配置 token 时放行（本地开发友好）。"""
    token = get_hub().settings.api_token
    if not token:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    if authorization.split(" ", 1)[1].strip() != token:
        raise HTTPException(status_code=403, detail="Token 无效")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = hub.settings
    if s.require_auth and not s.api_token:
        raise RuntimeError("A2A_REQUIRE_AUTH=true 但未设置 A2A_API_TOKEN")
    log.info("A2A Hub 启动：%s:%s（%d 个 agent）", s.host, s.port, len(hub.registry.list_records()))
    # 启动时做一次非阻塞健康探测，让控制台一打开就有状态
    asyncio.create_task(hub.registry.check_health(force=True))
    yield
    await hub.social.aclose()
    await hub.registry.aclose()


app = FastAPI(
    title="A2A Hub",
    description="异构 AI Agent 互联互通与多智能体协同网关",
    version="0.3.0",
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
    return hub.registry.hub_card()


@app.get("/agents", tags=["发现"])
async def list_agents(refresh: bool = Query(default=False), auth: None = Depends(require_auth)) -> dict[str, Any]:
    if refresh:
        await hub.registry.check_health(force=True)
    return {"count": len(hub.registry.list_records()), "agents": hub.registry.snapshot()}


@app.get("/agents/{agent_id}/.well-known/agent.json", tags=["发现"])
@app.get("/agents/{agent_id}/.well-known/agent-card.json", tags=["发现"], include_in_schema=False)
async def agent_card(agent_id: str) -> dict[str, Any]:
    try:
        return hub.registry.agent_card(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/agents/{agent_id}", tags=["发现"])
async def agent_detail(agent_id: str, auth: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        rec = hub.registry.get(agent_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return rec.snapshot(hub.settings.public_url)


@app.post("/agents/{agent_id}/", tags=["A2A"])
async def agent_scoped_rpc(
    agent_id: str, request: dict[str, Any] = Body(...), auth: None = Depends(require_auth)
) -> Any:
    return await _handle_rpc(request, scoped_agent=agent_id)


# --------------------------------------------------------------------------- #
# A2A JSON-RPC 入口
# --------------------------------------------------------------------------- #


@app.post("/", tags=["A2A"])
async def jsonrpc_entry(
    request: dict[str, Any] = Body(...), auth: None = Depends(require_auth)
) -> Any:
    return await _handle_rpc(request, scoped_agent=None)


async def _handle_rpc(request: dict[str, Any], scoped_agent: Optional[str]) -> Any:
    """统一入口。method 需要流式时返回 SSE，否则返回 JSON。"""
    method = request.get("method")

    if method == "message/stream":
        return await _stream_response(
            hub.dispatcher.handle(request, scoped_agent),
            method_name="message/stream",
        )
    if method == "tasks/resubscribe":
        return await _stream_response(
            hub.dispatcher.handle(request, scoped_agent),
            method_name="tasks/resubscribe",
        )

    result = await hub.dispatcher.handle(request, scoped_agent)

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
    limit: int = Query(default=50, ge=1, le=500), auth: None = Depends(require_auth)
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
    auth: None = Depends(require_auth),
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
async def cancel_task(task_id: str, auth: None = Depends(require_auth)) -> dict[str, Any]:
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
    payload: dict[str, Any] = Body(...), auth: None = Depends(require_auth)
) -> dict[str, Any]:
    mode = payload.get("mode") or "broadcast"
    prompt = payload.get("prompt")
    if not prompt:
        raise HTTPException(status_code=422, detail="缺少 prompt")

    try:
        run = hub.orchestrator.create_run(
            mode, prompt, payload.get("agentIds") or [], payload.get("options") or {}
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.get("blocking", False):
        await hub.orchestrator.run_sync(run)
    else:
        await hub.orchestrator.launch(run)
    return run.model_dump(mode="json")


@app.get("/collab", tags=["协同"])
async def list_collab(
    limit: int = Query(default=30, ge=1, le=200), auth: None = Depends(require_auth)
) -> dict[str, Any]:
    runs = hub.orchestrator.list_runs(limit)
    return {"count": len(runs), "runs": [r.model_dump(mode="json") for r in runs]}


@app.get("/collab/{run_id}", tags=["协同"])
async def get_collab(run_id: str, auth: None = Depends(require_auth)) -> dict[str, Any]:
    run = hub.orchestrator.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未找到协同运行 {run_id}")
    return run.model_dump(mode="json")


@app.delete("/collab/{run_id}", tags=["协同"])
async def cancel_collab(run_id: str, auth: None = Depends(require_auth)) -> dict[str, Any]:
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


@app.get("/im/contacts", tags=["会话"])
async def im_contacts(auth: None = Depends(require_auth)) -> dict[str, Any]:
    """通讯录：可聊的 agent 列表 + 在线状态。"""
    items = hub.social.contacts()
    return {"count": len(items), "contacts": items}


@app.get("/im/conversations", tags=["会话"])
async def im_list_conversations(auth: None = Depends(require_auth)) -> dict[str, Any]:
    """会话列表（带最后一条消息与未读数）——相当于微信首页。"""
    items = hub.social.list_conversations()
    return {"count": len(items), "conversations": items}


@app.post("/im/conversations", tags=["会话"])
async def im_create_conversation(
    payload: dict[str, Any] = Body(...), auth: None = Depends(require_auth)
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
            conv = hub.social.open_direct(str(agent_id))
        elif kind == "group":
            members = payload.get("members") or payload.get("agentIds") or []
            if not members:
                raise HTTPException(status_code=422, detail="群聊需要 `members` 参数")
            conv = hub.social.create_group(
                [str(m) for m in members],
                str(payload.get("title") or ""),
                bool(payload.get("autoRoute", True)),
            )
        else:
            raise HTTPException(status_code=422, detail=f"未知会话类型 `{kind}`")
    except A2AError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return hub.social.summary(conv)


@app.get("/im/conversations/{conv_id}", tags=["会话"])
async def im_get_conversation(
    conv_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """拉取聊天记录（含每条消息的投递回执）。"""
    try:
        return hub.social.history(conv_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/im/conversations/{conv_id}/messages", tags=["会话"])
async def im_send_message(
    conv_id: str,
    payload: dict[str, Any] = Body(...),
    auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """发消息。

    **立即返回**：消息先落库并广播，被唤醒的 agent 在后台跑完再回话——
    要拿到回复请订阅 ``GET /im/conversations/{id}/events``。
    """
    text = payload.get("text") or payload.get("content") or ""
    try:
        return await hub.social.send(
            conv_id,
            str(text),
            sender=str(payload.get("sender") or USER),
            reply_to=payload.get("replyTo"),
            wake=payload.get("wake"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/im/conversations/{conv_id}/read", tags=["会话"])
async def im_mark_read(
    conv_id: str,
    payload: Optional[dict[str, Any]] = Body(default=None),
    auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """清空未读红点。"""
    reader = str((payload or {}).get("reader") or USER)
    try:
        conv = hub.social.mark_read(conv_id, reader)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return hub.social.summary(conv)


@app.patch("/im/conversations/{conv_id}", tags=["会话"])
async def im_update_conversation(
    conv_id: str,
    payload: dict[str, Any] = Body(...),
    auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """群聊拉人 / 踢人：``{"add": ["echo"], "remove": ["static"]}``。"""
    try:
        conv = hub.social.update_group(conv_id, payload.get("add"), payload.get("remove"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, A2AError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return hub.social.summary(conv)


@app.delete("/im/conversations/{conv_id}", tags=["会话"])
async def im_disband(conv_id: str, auth: None = Depends(require_auth)) -> dict[str, Any]:
    if not hub.social.disband(conv_id):
        raise HTTPException(status_code=404, detail=f"未找到会话 {conv_id}")
    return {"disbanded": True, "conversationId": conv_id}


@app.get("/im/conversations/{conv_id}/events", tags=["会话"])
async def im_events(request: Request, conv_id: str) -> StreamingResponse:
    """会话实时事件流：新消息 / 回执变更 / 心跳。"""
    if not hub.social.has(conv_id):
        raise HTTPException(status_code=404, detail=f"未找到会话 {conv_id}")

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
# Web 控制台
# --------------------------------------------------------------------------- #


@app.get("/console", include_in_schema=False)
@app.get("/console/", include_in_schema=False)
async def console() -> Any:
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="控制台静态文件缺失")
    return FileResponse(index)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
