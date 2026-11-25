"""Restricted outbound-poll API for the WorkBuddy Personal connector."""
from __future__ import annotations

import asyncio
import functools
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .ports import PullTaskRepository
from .store import task_to_wire
from .github_result_callback import GithubResultDeliveryError, deliver_github_issue_result
from .github_callback_outbox import GithubCallbackOutbox
from .workbuddy_presence import WorkBuddyPresenceStore
from .rate_limit_store import SQLiteRateLimitStore
from .workbuddy_audit import emit as emit_audit, fingerprint as token_fingerprint
from .workbuddy_runtime import (SUPPORTED_PROTOCOL_VERSIONS, negotiate_protocol, classify_connection_age, recovery_advice)
from .workbuddy_capabilities import negotiate_capabilities


# Protocol handshake and lightweight liveness receipts. The connector token remains
# the sole authentication secret; the response never echoes it or the lease token.
class HandshakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    protocol_version: str = Field(default="1.0", min_length=1, max_length=16, pattern=r"^\d+\.\d+$")
    client_version: str = Field(default="unknown", min_length=1, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=20)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    heartbeat_id: str = Field(min_length=8, max_length=128)
    heartbeat_seq: int | None = Field(default=None, ge=1, strict=True)
    status: Literal["idle", "busy", "degraded"]
    current_task_id: str | None = Field(default=None, max_length=128)


def _server_time() -> str:
    return datetime.now(timezone.utc).isoformat()



class ConnectionErrorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    error_code: Literal[
        "network_unreachable", "timeout", "authentication_failed",
        "protocol_mismatch", "hub_unavailable", "unknown",
    ]


class ReconnectAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_ids: list[str] = Field(default_factory=list, max_length=50)
    lease_seconds: int | None = Field(default=None, ge=15, le=900)


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=256)
    lease_version: int = Field(ge=1)


class ProgressRequest(LeaseRequest):
    sequence: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=4000)


class ResultRequest(LeaseRequest):
    result_key: str = Field(min_length=8, max_length=128)
    state: Literal["completed", "failed"]
    output: str = Field(default="", max_length=20000)


def build_workbuddy_connector_router(get_hub: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/integrations/workbuddy/v1", tags=["WorkBuddy connector"])
    rate_stores: dict[str, SQLiteRateLimitStore] = {}

    def runtime():
        hub = get_hub()
        settings = hub.settings
        expected = settings.workbuddy_connector_token
        if not expected or len(expected) < 32:
            raise HTTPException(status_code=503, detail="WorkBuddy connector 尚未配置有效的独立令牌（至少 32 个字符）")
        interval = settings.workbuddy_heartbeat_interval_seconds
        ttl = settings.workbuddy_presence_ttl_seconds
        if interval < 5 or interval > 300 or ttl < 30 or ttl > 3600 or ttl < interval:
            raise HTTPException(status_code=503, detail="WorkBuddy heartbeat interval/TTL configuration is invalid")
        return hub, settings

    def authenticate(token: str | None, settings: Any) -> None:
        if not token:
            raise HTTPException(status_code=401, detail="缺少 X-WorkBuddy-Connector-Token")
        if not hmac.compare_digest(token, settings.workbuddy_connector_token):
            raise HTTPException(status_code=403, detail="WorkBuddy connector token 无效")

    def rate_store_for(settings: Any) -> SQLiteRateLimitStore:
        path = str(settings.workbuddy_rate_limit_db_path_resolved())
        store = rate_stores.get(path)
        if store is None:
            store = SQLiteRateLimitStore(path)
            rate_stores[path] = store
        return store

    async def guard(token: str | None, settings: Any) -> None:
        authenticate(token, settings)
        # Namespace, credential, and connector identity define the bucket.
        # The numeric budget is policy, not identity: changing it must not reset
        # an active window. Raw credentials never reach the rate-limit database.
        import hashlib
        bucket = hashlib.sha256(
            ("workbuddy\\0" + settings.workbuddy_connector_token + "\\0"
             + settings.workbuddy_connector_id).encode("utf-8")
        ).hexdigest()
        try:
            allowed = await asyncio.to_thread(
                rate_store_for(settings).allow,
                bucket,
                settings.workbuddy_connector_rate_limit,
                60,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="限流存储暂不可用，请稍后重试") from exc
        if not allowed:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")

    def audited(event: str):
        def decorate(fn):
            @functools.wraps(fn)
            async def wrapped(*args, **kwargs):
                started = time.monotonic()
                supplied_token = kwargs.get("connector_token")
                try:
                    response = await fn(*args, **kwargs)
                except HTTPException as exc:
                    status = exc.status_code
                    if status == 401:
                        outcome, reason = "unauthenticated", "token_missing"
                    elif status == 403:
                        outcome = "denied"
                        reason = (
                            "connector_identity_mismatch"
                            if exc.detail == "WorkBuddy connector identity mismatch"
                            else "token_mismatch"
                        )
                    elif status == 409:
                        outcome, reason = "conflict", "conflict"
                    elif status == 429:
                        outcome, reason = "rate_limited", "rate_limited"
                    elif status == 503:
                        outcome, reason = "unavailable", "unavailable"
                    else:
                        outcome, reason = "error", "http_error"
                    try:
                        connector_id = get_hub().settings.workbuddy_connector_id
                    except Exception:
                        connector_id = None
                    emit_audit(
                        event, outcome, connector_id=connector_id, status=status,
                        token_fp=token_fingerprint(supplied_token),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        reason=reason,
                    )
                    raise
                except Exception:
                    try:
                        connector_id = get_hub().settings.workbuddy_connector_id
                    except Exception:
                        connector_id = None
                    emit_audit(
                        event, "error", connector_id=connector_id, status=500,
                        token_fp=token_fingerprint(supplied_token),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        reason="internal_error",
                    )
                    raise
                try:
                    connector_id = get_hub().settings.workbuddy_connector_id
                except Exception:
                    connector_id = None
                emit_audit(
                    event, "ok", connector_id=connector_id, status=200,
                    token_fp=token_fingerprint(supplied_token),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                return response
            return wrapped
        return decorate

    def store_for(hub: Any) -> PullTaskRepository:
        if not isinstance(hub.registry.store, PullTaskRepository):
            raise HTTPException(status_code=503, detail="Connector claim requires a task repository that supports the PullTaskRepository contract")
        return hub.registry.store

    def outbox_for(settings: Any) -> GithubCallbackOutbox:
        path = Path(settings.github_callback_outbox_db_path)
        if not path.is_absolute():
            from .config import ROOT
            path = (ROOT / path).resolve()
        return GithubCallbackOutbox(path, max_attempts=settings.github_callback_outbox_max_attempts)

    def presence_for(settings: Any) -> WorkBuddyPresenceStore:
        path = Path(settings.workbuddy_presence_db_path)
        if not path.is_absolute():
            from .config import ROOT
            path = (ROOT / path).resolve()
        return WorkBuddyPresenceStore(path, ttl_seconds=settings.workbuddy_presence_ttl_seconds)

    def lease_args(body: LeaseRequest, settings: Any) -> tuple[str, str, int, int]:
        return settings.workbuddy_connector_id, body.lease_token, body.lease_version, settings.workbuddy_connector_lease_seconds

    @router.post("/claim")
    @audited("claim")
    async def claim(
        body: ClaimRequest | None = None,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        store = store_for(hub)
        configured = {x.strip() for x in settings.workbuddy_connector_allowed_agents.split(",") if x.strip()}
        if not configured:
            raise HTTPException(
                status_code=503,
                detail="WorkBuddy connector agent allowlist is not configured",
            )
        requested = set((body or ClaimRequest()).agent_ids)
        allowed = sorted(configured.intersection(requested)) if requested else sorted(configured)
        seconds = (body.lease_seconds if body and body.lease_seconds else settings.workbuddy_connector_lease_seconds)
        claimed = store.claim_next(settings.workbuddy_connector_id, seconds, allowed)
        if claimed is None:
            return {"claimed": False}
        return {
            "claimed": True,
            "task": task_to_wire(claimed["task"], include_internal=True),
            "lease": {
                "connector_id": claimed["connector_id"],
                "lease_token": claimed["lease_token"],
                "lease_version": claimed["lease_version"],
                "lease_expires_at": claimed["lease_expires_at"],
            },
        }

    @router.post("/tasks/{task_id}/start")
    @audited("start")
    async def start(
        task_id: str,
        body: LeaseRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        connector_id, token, version, _ = lease_args(body, settings)
        try:
            task = store_for(hub).start_pull_task(task_id, connector_id, token, version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        hub.registry.bus.publish(task_id, task)
        return task_to_wire(task, include_internal=True)

    @router.post("/tasks/{task_id}/renew")
    @audited("renew")
    async def renew(
        task_id: str,
        body: LeaseRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        connector_id, token, version, seconds = lease_args(body, settings)
        try:
            expires = store_for(hub).renew_pull_lease(task_id, connector_id, token, version, seconds)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"lease_expires_at": expires}

    @router.post("/tasks/{task_id}/progress")
    @audited("progress")
    async def progress(
        task_id: str,
        body: ProgressRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        connector_id, token, version, _ = lease_args(body, settings)
        try:
            task, replay = store_for(hub).progress_pull_task(
                task_id, connector_id, token, version, body.sequence, body.message
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not replay:
            hub.registry.bus.publish(task_id, task)
        return {"task": task_to_wire(task, include_internal=True), "idempotent_replay": replay}

    @router.post("/tasks/{task_id}/result")
    @audited("result")
    async def result(
        task_id: str,
        body: ResultRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        connector_id, token, version, _ = lease_args(body, settings)
        try:
            task, replay = store_for(hub).finish_pull_task(
                task_id, connector_id, token, version, body.result_key, body.state, body.output
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        callback_result = None
        callback = task.metadata.get("github_issue_callback")
        if isinstance(callback, dict):
            outbox = outbox_for(settings)
            # The terminal task is committed first; persist callback intent before network I/O.
            outbox.enqueue(task_id, callback, body.state, body.output)
            # A WorkBuddy replay can request an immediate retry. The atomic SQLite
            # lease still ensures only one process (request or worker) owns delivery.
            if replay:
                outbox.retry(task_id)
            item = outbox.claim_due()
            if item is None:
                callback_result = {"queued": True, "status": (outbox.status(task_id) or {}).get("status", "pending")}
            else:
                try:
                    callback_result = await asyncio.to_thread(
                        deliver_github_issue_result, item["callback"], task_id,
                        item["state"], item["output"],
                        settings.github_relay_api_token, settings.github_relay_allowed_repos,
                    )
                except GithubResultDeliveryError as exc:
                    outbox.mark_failed(task_id, item["attempts"], item["lease_token"], exc)
                    raise HTTPException(status_code=502, detail="GitHub 结果已进入持久化重试队列")
                else:
                    outbox.mark_delivered(task_id, item["lease_token"])
        if not replay:
            hub.registry.bus.publish(task_id, task)
            hub.registry.bus.close_task(task_id)
        response = {"task": task_to_wire(task, include_internal=True), "idempotent_replay": replay}
        if callback_result is not None:
            response["github_callback"] = callback_result
        return response

    @router.get("/callbacks/{task_id}")
    @audited("callback_status")
    async def callback_status(task_id: str, connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token")) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        status = outbox_for(settings).status(task_id)
        if status is None:
            raise HTTPException(status_code=404, detail="回调任务不存在")
        return status

    @router.post("/callbacks/{task_id}/retry")
    @audited("callback_retry")
    async def retry_callback(task_id: str, connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token")) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        if not outbox_for(settings).retry(task_id):
            raise HTTPException(status_code=409, detail="回调不存在或当前状态不可人工重试")
        return {"task_id": task_id, "status": "pending"}


    @router.post("/connection-events/error")
    @audited("connection_error")
    async def report_connection_error(
        body: ConnectionErrorRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        presence_for(settings).record_error(settings.workbuddy_connector_id, body.error_code)
        return {"accepted": True, "error_code": body.error_code}

    @router.post("/connection-events/reconnect-attempt")
    @audited("reconnect_attempt")
    async def report_reconnect_attempt(
        body: ReconnectAttemptRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        attempt = presence_for(settings).record_reconnect_attempt(settings.workbuddy_connector_id)
        return {"accepted": True, "reconnect_attempt": attempt}

    @router.post("/handshake")
    @audited("handshake")
    async def handshake(
        body: HandshakeRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        try:
            negotiated_protocol = negotiate_protocol(body.protocol_version)
        except ValueError:
            raise HTTPException(status_code=426,
                detail="Unsupported WorkBuddy connector protocol version",
                headers={"X-WorkBuddy-Supported-Protocols": ", ".join(SUPPORTED_PROTOCOL_VERSIONS)}) from None
        configured = sorted(
            item.strip() for item in settings.workbuddy_connector_allowed_agents.split(",")
            if item.strip()
        )
        if not configured:
            raise HTTPException(status_code=503, detail="WorkBuddy connector agent allowlist is not configured")
        try:
            negotiated_capabilities, unsupported_capabilities = negotiate_capabilities(body.capabilities)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        presence_for(settings).record_handshake(
            settings.workbuddy_connector_id,
            negotiated_protocol,
            body.client_version,
            negotiated_capabilities,
        )
        return {
            "accepted": True,
            "connector_id": settings.workbuddy_connector_id,
            "protocol_version": negotiated_protocol,
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "client_version": body.client_version,
            "server_time": _server_time(),
            "heartbeat_interval_seconds": settings.workbuddy_heartbeat_interval_seconds,
            "presence_ttl_seconds": settings.workbuddy_presence_ttl_seconds,
            "lease_seconds": settings.workbuddy_connector_lease_seconds,
            "allowed_agent_ids": configured,
            "capabilities": negotiated_capabilities,
            "unsupported_capabilities": unsupported_capabilities,
        }

    @router.post("/heartbeat")
    @audited("heartbeat")
    async def heartbeat(
        body: HeartbeatRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        await guard(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        recorded = presence_for(settings).record_heartbeat(
            settings.workbuddy_connector_id,
            body.heartbeat_id,
            body.status,
            body.current_task_id,
            body.heartbeat_seq,
        )
        if not recorded:
            raise HTTPException(
                status_code=409,
                detail="WorkBuddy connector must complete handshake and heartbeat sequence must increase",
            )
        cancel_requested = False
        if body.current_task_id and isinstance(hub.registry.store, PullTaskRepository):
            cancel_requested = hub.registry.store.is_pull_task_cancelled(
                body.current_task_id, settings.workbuddy_connector_id
            )
        return {
            "accepted": True,
            "connector_id": settings.workbuddy_connector_id,
            "receipt_id": body.heartbeat_id,
            "heartbeat_seq": body.heartbeat_seq,
            "status": body.status,
            "current_task_id": body.current_task_id,
            "cancel_requested": cancel_requested,
            "cancel_reason": "task_cancelled_by_caller" if cancel_requested else None,
            "server_time": _server_time(),
            "next_heartbeat_seconds": settings.workbuddy_heartbeat_interval_seconds,
            "presence_ttl_seconds": settings.workbuddy_presence_ttl_seconds,
        }

    @router.get("/presence")
    @audited("presence")
    async def presence(
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        state = presence_for(settings).get_presence(settings.workbuddy_connector_id)
        if state is None:
            return {
                "connector_id": settings.workbuddy_connector_id,
                "online": False,
                "status": "offline",
                "reason": "handshake_not_seen",
                "ttl_seconds": settings.workbuddy_presence_ttl_seconds,
            }
        return state

    @router.get("/diagnostics")
    @audited("diagnostics")
    async def diagnostics(
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        await guard(connector_token, settings)
        presence_store = presence_for(settings)
        state = presence_store.get_presence(settings.workbuddy_connector_id)
        events = presence_store.get_runtime_events(settings.workbuddy_connector_id)
        if state is None:
            return {
                "connector_id": settings.workbuddy_connector_id,
                "connection_state": "offline",
                "online": False,
                "reason": "handshake_not_seen",
                "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "heartbeat_interval_seconds": settings.workbuddy_heartbeat_interval_seconds,
                "presence_ttl_seconds": settings.workbuddy_presence_ttl_seconds,
                "last_error_code": events["last_error_code"],
                "last_error_at": events["last_error_at"],
                "reconnect_attempt": events["reconnect_attempt"],
                "recovery": recovery_advice({
                    "connection_state": "offline",
                    "reconnect_attempt": events["reconnect_attempt"],
                    "last_error_code": events["last_error_code"],
                }),
                "diagnostics_version": 3,
            }
        health = classify_connection_age(
            state.get("seconds_since_seen"),
            heartbeat_interval_seconds=settings.workbuddy_heartbeat_interval_seconds,
            presence_ttl_seconds=settings.workbuddy_presence_ttl_seconds,
        )
        recovery_state = (
            "offline"
            if events["last_error_code"] is not None or events["reconnect_attempt"] > 0
            else health["state"]
        )
        return {
            "connector_id": state["connector_id"],
            "connection_state": recovery_state,
            "online": state["online"] and recovery_state == "online",
            "status": "offline" if recovery_state == "offline" else state["status"],
            "protocol_version": state["protocol_version"],
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "client_version": state["client_version"],
            "capabilities": state["capabilities"],
            "last_heartbeat_id": state["last_heartbeat_id"],
            "last_heartbeat_seq": state["last_heartbeat_seq"],
            "last_seen_at": state["last_seen_at"],
            "seconds_since_seen": health["seconds_since_seen"],
            "heartbeat_interval_seconds": settings.workbuddy_heartbeat_interval_seconds,
            "presence_ttl_seconds": settings.workbuddy_presence_ttl_seconds,
            "last_error_code": events["last_error_code"],
            "last_error_at": events["last_error_at"],
            "reconnect_attempt": events["reconnect_attempt"],
            "recovery": recovery_advice({
                "connection_state": recovery_state,
                "reconnect_attempt": events["reconnect_attempt"],
                "last_error_code": events["last_error_code"],
            }),
            "diagnostics_version": 3,
        }

    return router
