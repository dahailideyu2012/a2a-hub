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

from . import __version__ as APP_VERSION
from .autonomy import SocialCruise
from .config import get_settings
from .models import A2AError, JsonRpcError, JsonRpcErrorCodes, JsonRpcResponse, TaskStatus, utc_now
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
        self.dispatcher = JsonRpcDispatcher(self.registry, self.orchestrator, self.social)
        self.cruise: Optional[SocialCruise] = None
        self._workbuddy_queues: dict[str, asyncio.Queue] = {}
        self._workbuddy_leases: dict[str, str] = {}  # task_id -> worker_id

    def _build_graph(self) -> SocialGraph:
        graph = SocialGraph(self.registry.store)
        bootstrap_members(graph)
        return graph


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    hub: Hub = app.state.hub
    log.info(f"A2AHub v{APP_VERSION} starting up...")
    
    # 启动巡航/自治后台任务（如有）
    if hub.settings.enable_cruise:
        hub.cruise = SocialCruise(hub.social, hub.social_graph, hub.registry)
        await hub.cruise.start()
        
    yield
    
    if hub.cruise:
        await hub.cruise.stop()
    log.info("A2AHub shutting down...")


app = FastAPI(
    title="A2A Hub & ChatGPT ↔ WorkBuddy Bridge",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

hub_instance = Hub()
app.state.hub = hub_instance


# --------------------------------------------------------------------------- #
# 认证依赖与辅助
# --------------------------------------------------------------------------- #

def verify_token(authorization: Optional[str] = Header(None)) -> str:
    settings = get_settings()
    if not settings.auth_token:
        return "anonymous"
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    token = parts[1]
    if not hmac.compare_digest(token, settings.auth_token):
        raise HTTPException(status_code=403, detail="Invalid API token")
    return "authenticated"


# --------------------------------------------------------------------------- #
# 协议与状态端点
# --------------------------------------------------------------------------- #

@app.get("/.well-known/agent.json")
@app.get("/.well-known/agent-card.json")
async def agent_card() -> dict[str, Any]:
    return manifest.get_agent_card()


@app.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    agents = await hub.registry.list_agents()
    return [agent.to_dict() for agent in agents]


@app.get("/agents/{agent_id}/.well-known/agent.json")
async def agent_specific_card(agent_id: str) -> dict[str, Any]:
    hub: Hub = app.state.hub
    agent = await hub.registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    card = manifest.get_agent_card()
    card["name"] = agent.name
    card["description"] = agent.description
    return card


# --------------------------------------------------------------------------- #
# A2A 协议层（JSON-RPC 2.0）
# --------------------------------------------------------------------------- #

@app.post("/")
async def rpc_entry(request: Request, _: str = Depends(verify_token)) -> JsonRpcResponse:
    hub: Hub = app.state.hub
    try:
        body = await request.json()
    except Exception:
        return JsonRpcResponse(
            id=None,
            error=JsonRpcError(code=JsonRpcErrorCodes.PARSE_ERROR, message="Invalid JSON")
        )
    return await hub.dispatcher.dispatch(body)


@app.post("/agents/{agent_id}/")
async def rpc_agent_entry(agent_id: str, request: Request, _: str = Depends(verify_token)) -> JsonRpcResponse:
    hub: Hub = app.state.hub
    agent = await hub.registry.get_agent(agent_id)
    if not agent:
        return JsonRpcResponse(
            id=None,
            error=JsonRpcError(code=JsonRpcErrorCodes.INVALID_PARAMS, message=f"Agent {agent_id} not found")
        )
    try:
        body = await request.json()
    except Exception:
        return JsonRpcResponse(
            id=None,
            error=JsonRpcError(code=JsonRpcErrorCodes.PARSE_ERROR, message="Invalid JSON")
        )
    return await hub.dispatcher.dispatch(body, target_agent=agent_id)


# --------------------------------------------------------------------------- #
# 任务层（ChatGPT ↔ WorkBuddy 双向通信闭环 V1.0 核心实现）
# --------------------------------------------------------------------------- #

@app.post("/tasks")
async def submit_task(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    """ChatGPT 提交任务"""
    hub: Hub = app.state.hub
    task_id = payload.get("task_id") or f"task_{utc_now().strftime('%Y%m%d%H%M%S')}_{os.urandom(2).hex()}" if 'os' in globals() else f"task_{utc_now().timestamp()}"
    import uuid
    if not payload.get("task_id"):
        task_id = f"task_{uuid.uuid4().hex[:8]}"
    
    task_data = {
        "task_id": task_id,
        "title": payload.get("title", "Untitled Task"),
        "description": payload.get("description", ""),
        "status": TaskStatus.PENDING.value,
        "progress": 0,
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
        "input": payload.get("input", {}),
        "result": None,
        "error": None,
        "events": [{"timestamp": utc_now().isoformat(), "type": "CREATED", "message": "Task submitted by ChatGPT"}]
    }
    
    # 持久化到 store
    await hub.registry.store.save_task(task_id, task_data)
    
    # 广播事件或放入队列
    return {"status": "success", "task": task_to_wire(task_data)}


@app.get("/tasks")
async def list_tasks(limit: int = Query(50, ge=1, le=200), _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    tasks = await hub.registry.store.list_tasks(limit=limit)
    return [task_to_wire(t) for t in tasks]


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    task = await hub.registry.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_to_wire(task)


@app.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    """WorkBuddy Connector 主动领取任务"""
    hub: Hub = app.state.hub
    worker_id = payload.get("worker_id", "default_worker")
    task = await hub.registry.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task["status"] != TaskStatus.PENDING.value:
        raise HTTPException(status_code=400, detail=f"Task is not PENDING (current: {task['status']})")
    
    task["status"] = TaskStatus.CLAIMED.value
    task["worker_id"] = worker_id
    task["updated_at"] = utc_now().isoformat()
    task["events"].append({"timestamp": utc_now().isoformat(), "type": "CLAIMED", "message": f"Claimed by worker {worker_id}"})
    
    await hub.registry.store.save_task(task_id, task)
    return {"status": "success", "task": task_to_wire(task)}


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    """WorkBuddy 更新任务状态、进度或完成"""
    hub: Hub = app.state.hub
    task = await hub.registry.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    status = payload.get("status")
    if status:
        task["status"] = status
    if "progress" in payload:
        task["progress"] = payload["progress"]
    if "result" in payload:
        task["result"] = payload["result"]
    if "error" in payload:
        task["error"] = payload["error"]
        
    msg = payload.get("message", f"Task updated to {status}")
    task["updated_at"] = utc_now().isoformat()
    task["events"].append({"timestamp": utc_now().isoformat(), "type": status or "UPDATED", "message": msg})
    
    await hub.registry.store.save_task(task_id, task)
    return {"status": "success", "task": task_to_wire(task)}


@app.get("/tasks/{task_id}/events")
async def get_task_events(task_id: str, request: Request, _: str = Depends(verify_token)) -> StreamingResponse:
    """SSE 订阅任务事件"""
    hub: Hub = app.state.hub
    task = await hub.registry.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator() -> AsyncIterator[str]:
        sent_count = 0
        while True:
            if await request.is_disconnected():
                break
            current_task = await hub.registry.store.get_task(task_id)
            if current_task:
                events = current_task.get("events", [])
                while sent_count < len(events):
                    yield sse_format(events[sent_count], event="task_event")
                    sent_count += 1
                if current_task.get("status") in [TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]:
                    break
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), headers=SSE_HEADERS, media_type="text/event-stream")


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    task = await hub.registry.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task["status"] = TaskStatus.CANCELLED.value
    task["updated_at"] = utc_now().isoformat()
    task["events"].append({"timestamp": utc_now().isoformat(), "type": "CANCELLED", "message": "Task cancelled by user/ChatGPT"})
    
    await hub.registry.store.save_task(task_id, task)
    return {"status": "success", "task": task_to_wire(task)}


# --------------------------------------------------------------------------- #
# 协同、会话与社交层保持原样
# --------------------------------------------------------------------------- #

@app.post("/collab")
async def start_collab(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    goal = payload.get("goal", "")
    agents = payload.get("agents")
    run = await hub.orchestrator.run(goal, agents=agents)
    return run.to_dict()


@app.get("/collab")
async def list_collabs(_: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    runs = await hub.orchestrator.list_runs()
    return [r.to_dict() for r in runs]


@app.get("/collab/{run_id}")
async def get_collab(run_id: str, _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    run = await hub.orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Collab run not found")
    return run.to_dict()


@app.get("/collab/{run_id}/events")
async def collab_events(run_id: str, request: Request, _: str = Depends(verify_token)) -> StreamingResponse:
    hub: Hub = app.state.hub
    run = await hub.orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Collab run not found")

    async def event_generator() -> AsyncIterator[str]:
        async for ev in hub.orchestrator.subscribe(run_id):
            if await request.is_disconnected():
                break
            yield sse_format(ev)

    return StreamingResponse(event_generator(), headers=SSE_HEADERS, media_type="text/event-stream")


@app.delete("/collab/{run_id}")
async def cancel_collab(run_id: str, _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    ok = await hub.orchestrator.cancel_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Collab run not found or already finished")
    return {"status": "cancelled", "run_id": run_id}


# IM 与 Social 端点
@app.get("/im/contacts")
async def im_contacts(_: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    agents = await hub.registry.list_agents()
    return [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description,
            "online": True,
            "card": a.to_dict(),
        }
        for a in agents
    ]


@app.get("/im/conversations")
async def im_list_conversations(user: str = Query(USER), _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    convs = await hub.social.list_conversations(user)
    return [c.to_dict() for c in convs]


@app.post("/im/conversations")
async def im_create_conversation(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    user = payload.get("user", USER)
    participants = payload.get("participants", [])
    title = payload.get("title")
    conv = await hub.social.create_conversation(user, participants, title=title)
    return conv.to_dict()


@app.get("/im/conversations/{conv_id}")
async def im_get_conversation(conv_id: str, user: str = Query(USER), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    conv = await hub.social.get_conversation(conv_id, user)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv.to_dict()


@app.post("/im/conversations/{conv_id}/messages")
async def im_send_message(conv_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    user = payload.get("user", USER)
    content = payload.get("content", "")
    msg = await hub.social.send_message(conv_id, user, content)
    return msg.to_dict()


@app.post("/im/conversations/{conv_id}/read")
async def im_mark_read(conv_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    user = payload.get("user", USER)
    await hub.social.mark_read(conv_id, user)
    return {"status": "ok"}


@app.get("/im/conversations/{conv_id}/events")
async def im_conversation_events(conv_id: str, user: str = Query(USER), request: Request = None, _: str = Depends(verify_token)) -> StreamingResponse:
    hub: Hub = app.state.hub

    async def event_generator() -> AsyncIterator[str]:
        async for ev in hub.social.subscribe_conversation(conv_id, user):
            if request and await request.is_disconnected():
                break
            yield sse_format(ev)

    return StreamingResponse(event_generator(), headers=SSE_HEADERS, media_type="text/event-stream")


@app.get("/social/me")
async def social_me(user: str = Query(USER), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    brief = social_briefing(hub.social_graph, user)
    return brief


@app.get("/social/members")
async def social_members(q: Optional[str] = Query(None), _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    all_m = hub.social_graph.list_members()
    if q:
        ql = q.lower()
        all_m = [m for m in all_m if ql in m.id.lower() or ql in m.name.lower()]
    return [m.to_dict() for m in all_m]


@app.get("/social/relations")
async def social_relations(user: str = Query(USER), _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    rels = hub.social_graph.list_relations(user)
    return [r.to_dict() for r in rels]


@app.post("/social/requests")
async def social_send_request(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    sender = payload.get("sender", USER)
    receiver = payload.get("receiver")
    msg = payload.get("message", "")
    try:
        req = hub.social_graph.send_friend_request(sender, receiver, msg)
        return req.to_dict()
    except SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/social/requests")
async def social_list_requests(user: str = Query(USER), _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub: Hub = app.state.hub
    reqs = hub.social_graph.list_friend_requests(user)
    return [r.to_dict() for r in reqs]


@app.post("/social/requests/accept")
async def social_accept_request(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    request_id = payload.get("request_id")
    user = payload.get("user", USER)
    try:
        hub.social_graph.accept_friend_request(request_id, user)
        return {"status": "accepted"}
    except SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/social/requests/reject")
async def social_reject_request(payload: dict[str, Any] = Body(...), _: str = Depends(verify_token)) -> dict[str, Any]:
    hub: Hub = app.state.hub
    request_id = payload.get("request_id")
    user = payload.get("user", USER)
    try:
        hub.social_graph.reject_friend_request(request_id, user)
        return {"status": "rejected"}
    except SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/console")
async def console_home() -> FileResponse:
    index_html = WEB_DIR / "index.html"
    if not index_html.exists():
        raise HTTPException(status_code=404, detail="Web console not built or missing index.html")
    return FileResponse(index_html)


if WEB_DIR.exists() and (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")