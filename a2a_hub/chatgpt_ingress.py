"""Restricted REST ingress for ChatGPT; deliberately not a Remote MCP server."""
from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, re, sqlite3, time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Literal
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .bus import STREAM_END
from .store import task_to_wire
from .ports import RepositoryConflictError, RepositoryNotFoundError

log = logging.getLogger("a2a_hub.chatgpt_ingress")
_IDEMPOTENCY_TTL = 24 * 60 * 60
_PENDING_TTL = 120
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

class DelegateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    prompt: str = Field(min_length=1, max_length=12000)
    mode: Literal["hub", "pull"] = "hub"

class IdempotencyConflict(Exception): pass
class IdempotencyPending(Exception): pass

class IngressLedger:
    """SQLite ledger for idempotency and ingress-owned task visibility."""
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.execute("""CREATE TABLE IF NOT EXISTS chatgpt_ingress_requests (
            idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
            task_id TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chatgpt_ingress_created ON chatgpt_ingress_requests(created_at DESC)")
        return conn
    async def reserve(self, key: str, request_hash: str) -> tuple[str, str | None]:
        async with self._lock:
            conn = self._connect()
            try:
                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM chatgpt_ingress_requests WHERE expires_at < ?", (now,))
                row = conn.execute("SELECT request_hash, task_id FROM chatgpt_ingress_requests WHERE idempotency_key=?", (key,)).fetchone()
                if row:
                    if row[0] != request_hash: raise IdempotencyConflict
                    if row[1]:
                        conn.commit()
                        return "replay", str(row[1])
                    raise IdempotencyPending
                conn.execute("INSERT INTO chatgpt_ingress_requests(idempotency_key,request_hash,task_id,created_at,expires_at) VALUES(?,?,NULL,?,?)", (key, request_hash, now, now + _PENDING_TTL))
                conn.commit()
                return "reserved", None
            except Exception:
                conn.rollback()
                raise
            finally: conn.close()
    async def complete(self, key: str, request_hash: str, task_id: str) -> None:
        """Bind a committed task even if its short-lived reservation was cleaned up.

        The task repository's idempotency mapping is authoritative for recovery.
        This ledger write is an idempotent insert-or-bind operation, but it must
        never overwrite a different request hash or task ID.
        """
        async with self._lock:
            conn = self._connect()
            try:
                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT request_hash, task_id FROM chatgpt_ingress_requests WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if row is not None and row[0] != request_hash:
                    raise IdempotencyConflict
                if row is not None and row[1] is not None and str(row[1]) != task_id:
                    raise IdempotencyConflict
                if row is None:
                    conn.execute(
                        "INSERT INTO chatgpt_ingress_requests(idempotency_key,request_hash,task_id,created_at,expires_at) VALUES(?,?,?,?,?)",
                        (key, request_hash, task_id, now, now + _IDEMPOTENCY_TTL),
                    )
                else:
                    conn.execute(
                        "UPDATE chatgpt_ingress_requests SET task_id=?, expires_at=? WHERE idempotency_key=? AND request_hash=?",
                        (task_id, now + _IDEMPOTENCY_TTL, key, request_hash),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    async def release(self, key: str, request_hash: str) -> None:
        async with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM chatgpt_ingress_requests WHERE idempotency_key=? AND request_hash=? AND task_id IS NULL", (key, request_hash))
                conn.commit()
            finally: conn.close()
    async def owns_task(self, task_id: str) -> bool:
        async with self._lock:
            conn = self._connect()
            try:
                return conn.execute("SELECT 1 FROM chatgpt_ingress_requests WHERE task_id=? AND expires_at>=? LIMIT 1", (task_id, time.time())).fetchone() is not None
            finally: conn.close()
    async def pending_requests(self, limit: int) -> list[tuple[str, str]]:
        async with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT idempotency_key, request_hash FROM chatgpt_ingress_requests "
                    "WHERE task_id IS NULL AND expires_at>=? ORDER BY created_at ASC LIMIT ?",
                    (time.time(), limit),
                ).fetchall()
                return [(str(row[0]), str(row[1])) for row in rows]
            finally: conn.close()
    async def task_ids(self, limit: int) -> list[str]:
        async with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT task_id FROM chatgpt_ingress_requests WHERE task_id IS NOT NULL AND expires_at>=? ORDER BY created_at DESC LIMIT ?", (time.time(), limit)).fetchall()
                return [str(row[0]) for row in rows]
            finally: conn.close()

class SlidingWindowLimiter:
    """Per-process limiter; use shared storage before multi-worker deployment."""
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit, self.window = max(1, int(limit)), window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
    async def allow(self, identity: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            hits = self._hits[identity]
            while hits and now - hits[0] >= self.window: hits.popleft()
            if len(hits) >= self.limit: return False
            hits.append(now)
            return True

def _sse(data: Any, event: str) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

async def _read_limited_body(request: Request, maximum: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > maximum: raise HTTPException(status_code=413, detail="请求体超过允许大小")
        except ValueError: raise HTTPException(status_code=400, detail="Content-Length 无效")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum: raise HTTPException(status_code=413, detail="请求体超过允许大小")
        chunks.append(chunk)
    return b"".join(chunks)

def build_chatgpt_ingress_router(get_hub: Callable[[], Any]) -> APIRouter:
    """Resolve Hub/settings per request so app/test reconfiguration remains isolated."""
    router = APIRouter(prefix="/integrations/chatgpt/v1", tags=["ChatGPT ingress"])
    ledgers: dict[str, IngressLedger] = {}
    limiters: dict[tuple[str, str, int], SlidingWindowLimiter] = {}
    def runtime():
        hub = get_hub()
        settings = hub.settings
        db_path = str(settings.chatgpt_ingress_db_path_resolved())
        ledger = ledgers.setdefault(db_path, IngressLedger(db_path))
        limiter_key = (db_path, settings.chatgpt_ingress_token, settings.chatgpt_ingress_rate_limit)
        limiter = limiters.setdefault(limiter_key, SlidingWindowLimiter(settings.chatgpt_ingress_rate_limit))
        return hub, settings, ledger, limiter
    def authenticate(token: str | None, settings: Any) -> str:
        expected = settings.chatgpt_ingress_token
        if not expected or len(expected) < 32: raise HTTPException(status_code=503, detail="ChatGPT ingress 尚未配置有效的高熵令牌（至少 32 个字符）")
        if not token: raise HTTPException(status_code=401, detail="缺少 X-ChatGPT-Ingress-Token")
        if not hmac.compare_digest(token, expected): raise HTTPException(status_code=403, detail="Ingress token 无效")
        return hashlib.sha256(expected.encode("utf-8")).hexdigest()
    async def guard(token: str | None, settings: Any, limiter: SlidingWindowLimiter) -> None:
        identity = authenticate(token, settings)
        if not await limiter.allow(identity): raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")

    async def reconcile_pending(hub: Any, ledger: IngressLedger, limit: int = 100) -> None:
        """Repair accepted tasks whose separate ingress-ledger completion was interrupted."""
        for key, request_hash in await ledger.pending_requests(limit):
            operation_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
            try:
                task = hub.registry.store.find_idempotent_task(operation_id, request_hash)
            except (RepositoryConflictError, RepositoryNotFoundError):
                continue
            if task is not None:
                try:
                    await ledger.complete(key, request_hash, task.id)
                except IdempotencyConflict:
                    # Another request may have reused an expired key while this
                    # reconciliation pass was in flight. Never overwrite it.
                    continue

    @router.post("/tasks")
    def verify_github_relay_signature(request: Request, settings: Any, idempotency_key: str, raw: bytes) -> None:
        source = request.headers.get("x-a2a-relay-source")
        if source is None:
            return
        if source != "github-issue-comment":
            raise HTTPException(status_code=400, detail="不支持的 relay 来源")
        secret = settings.github_relay_hmac_secret
        if len(secret) < 32:
            raise HTTPException(status_code=503, detail="GitHub relay HMAC 尚未配置有效密钥")
        timestamp = request.headers.get("x-a2a-relay-timestamp", "")
        supplied = request.headers.get("x-a2a-relay-signature", "")
        try:
            issued_at = int(timestamp)
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="GitHub relay 签名时间无效")
        if abs(time.time() - issued_at) > 300:
            raise HTTPException(status_code=401, detail="GitHub relay 签名已过期")
        message = timestamp.encode("ascii") + b"." + idempotency_key.encode("ascii") + b"." + raw
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="GitHub relay 签名无效")

    async def submit_task(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), ingress_token: str | None = Header(default=None, alias="X-ChatGPT-Ingress-Token")) -> dict[str, Any]:
        hub, settings, ledger, limiter = runtime()
        await guard(ingress_token, settings, limiter)
        if not idempotency_key or not _KEY_RE.fullmatch(idempotency_key):
            raise HTTPException(status_code=422, detail="必须提供 8-128 位有效 Idempotency-Key")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(status_code=415, detail="仅支持 application/json")
        raw = await _read_limited_body(request, settings.chatgpt_ingress_max_body_bytes)
        verify_github_relay_signature(request, settings, idempotency_key, raw)
        try: payload = DelegateRequest.model_validate_json(raw)
        except ValidationError: raise HTTPException(status_code=422, detail="请求参数无效；仅允许 agent_id 和 prompt")
        allowed = {v.strip() for v in settings.chatgpt_ingress_allowed_agents.split(",") if v.strip()}
        if not allowed: raise HTTPException(status_code=503, detail="尚未配置 ChatGPT ingress agent 白名单")
        if payload.agent_id not in allowed: raise HTTPException(status_code=403, detail="该 agent 不在 ChatGPT ingress 白名单中")
        try: record = hub.registry.get(payload.agent_id)
        except Exception: raise HTTPException(status_code=403, detail="白名单 agent 当前不可用")
        if not record.spec.enabled: raise HTTPException(status_code=403, detail="白名单 agent 当前已禁用")
        canonical = json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        operation_id = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

        def find_existing_task():
            try:
                return hub.registry.store.find_idempotent_task(operation_id, request_hash)
            except RepositoryConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RepositoryNotFoundError as exc:
                raise HTTPException(status_code=410, detail="幂等键已关联的任务已不存在") from exc

        async def complete_ledger(task_id: str) -> None:
            try:
                await ledger.complete(idempotency_key, request_hash, task_id)
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail="Idempotency-Key 已关联其他请求或任务") from exc

        try:
            state, existing_task_id = await ledger.reserve(idempotency_key, request_hash)
        except IdempotencyConflict:
            raise HTTPException(status_code=409, detail="Idempotency-Key 已用于不同请求")
        except IdempotencyPending:
            recovered = find_existing_task()
            if recovered is None:
                raise HTTPException(status_code=409, detail="相同请求正在处理中，请稍后重试")
            await complete_ledger(recovered.id)
            return {"task": task_to_wire(recovered, include_internal=True), "idempotent_replay": True}
        if state == "replay" and existing_task_id:
            existing = hub.registry.store.get(existing_task_id)
            if existing is None: raise HTTPException(status_code=410, detail="幂等记录存在，但任务已不在任务存储中")
            return {"task": task_to_wire(existing, include_internal=True), "idempotent_replay": True}
        try:
            recovered = find_existing_task()
        except HTTPException:
            # The repository proves this request hash was not accepted. Do not
            # leave a fresh reservation pending after an idempotency conflict.
            await ledger.release(idempotency_key, request_hash)
            raise
        if recovered is not None:
            await complete_ledger(recovered.id)
            return {"task": task_to_wire(recovered, include_internal=True), "idempotent_replay": True}
        try:
            rpc_result = await hub.dispatcher.handle({
                "jsonrpc": "2.0", "id": "chatgpt-ingress",
                "method": "tasks/enqueue" if payload.mode == "pull" else "message/send",
                "params": {"agentId": payload.agent_id, "message": payload.prompt,
                           "configuration": {"blocking": False},
                           "metadata": {"source": "chatgpt-ingress", "mode": payload.mode,
                                       "chatgpt_ingress_operation_id": operation_id,
                                       "chatgpt_ingress_request_hash": request_hash}},
            }, actor=settings.chatgpt_ingress_actor)
            if "error" in rpc_result:
                error = rpc_result["error"]
                code = int(error.get("code", -32603))
                status = 403 if code == -32008 else 422 if code in (-32600, -32601, -32602) else 502
                raise HTTPException(status_code=status, detail={"code": code, "message": "任务未被接受"})
            task_data = rpc_result.get("result")
            task_id = task_data.get("id") if isinstance(task_data, dict) else None
            if not isinstance(task_id, str) or not task_id: raise HTTPException(status_code=502, detail="Hub 未返回有效任务 ID")
            await complete_ledger(task_id)
            task = hub.registry.store.get(task_id)
            if task is None: raise HTTPException(status_code=502, detail="任务已提交但无法读取任务状态")
            return {"task": task_to_wire(task, include_internal=True), "idempotent_replay": False}
        except Exception:
            # Keep the reservation when the task store proves acceptance may have
            # committed. A retry or task-list reconciliation can then bind the ledger.
            try:
                accepted = find_existing_task()
            except Exception:
                accepted = None
            if accepted is None:
                await ledger.release(idempotency_key, request_hash)
            raise

    @router.get("/tasks")
    async def list_tasks(limit: int = Query(default=20, ge=1, le=100), ingress_token: str | None = Header(default=None, alias="X-ChatGPT-Ingress-Token")) -> dict[str, Any]:
        hub, settings, ledger, limiter = runtime()
        await guard(ingress_token, settings, limiter)
        await reconcile_pending(hub, ledger, limit)
        tasks = []
        for task_id in await ledger.task_ids(limit):
            task = hub.registry.store.get(task_id)
            if task is not None: tasks.append(task_to_wire(task, include_internal=True))
        return {"count": len(tasks), "tasks": tasks}

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, ingress_token: str | None = Header(default=None, alias="X-ChatGPT-Ingress-Token")) -> dict[str, Any]:
        hub, settings, ledger, limiter = runtime()
        await guard(ingress_token, settings, limiter)
        if not await ledger.owns_task(task_id): raise HTTPException(status_code=404, detail="未找到该 ingress 任务")
        task = hub.registry.store.get(task_id)
        if task is None: raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return task_to_wire(task, include_internal=True)

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, ingress_token: str | None = Header(default=None, alias="X-ChatGPT-Ingress-Token")) -> dict[str, Any]:
        hub, settings, ledger, limiter = runtime()
        await guard(ingress_token, settings, limiter)
        if not await ledger.owns_task(task_id):
            raise HTTPException(status_code=404, detail="未找到该 ingress 任务")
        from .ports import PullTaskRepository
        if not isinstance(hub.registry.store, PullTaskRepository):
            raise HTTPException(status_code=409, detail="当前存储不支持 Pull 任务取消")
        try:
            task = hub.registry.store.cancel_pull_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        hub.registry.bus.publish(task_id, task)
        hub.registry.bus.close_task(task_id)
        return task_to_wire(task, include_internal=True)

    @router.get("/tasks/{task_id}/events")
    async def task_events(request: Request, task_id: str, ingress_token: str | None = Header(default=None, alias="X-ChatGPT-Ingress-Token")) -> StreamingResponse:
        hub, settings, ledger, limiter = runtime()
        await guard(ingress_token, settings, limiter)
        if not await ledger.owns_task(task_id): raise HTTPException(status_code=404, detail="未找到该 ingress 任务")
        task = hub.registry.store.get(task_id)
        if task is None: raise HTTPException(status_code=404, detail="任务不存在或已过期")
        async def source():
            bus = hub.registry.bus
            queue = bus.subscribe(task_id)
            try:
                current = hub.registry.store.get(task_id)
                if current is not None: yield _sse(task_to_wire(current, include_internal=True), "task")
                while current is not None and not current.status.is_terminal:
                    if await request.is_disconnected(): return
                    received, event = await bus.next_event(queue, timeout=20.0)
                    if not received:
                        current = hub.registry.store.get(task_id)
                        if current is not None and current.status.is_terminal:
                            yield _sse(task_to_wire(current, include_internal=True), "task")
                            break
                        yield ": keepalive\n\n"
                        continue
                    if event is STREAM_END: break
                    data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
                    kind = str(data.get("kind", "event")) if isinstance(data, dict) else "event"
                    yield _sse(data, kind)
                    current = hub.registry.store.get(task_id)
            finally: bus.unsubscribe(task_id, queue)
            yield _sse({"kind": "done"}, "done")
        return StreamingResponse(source(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    return router
