"""Restricted outbound-poll API for the WorkBuddy Personal connector."""
from __future__ import annotations

import asyncio
import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .ports import PullTaskRepository
from .store import task_to_wire
from .github_result_callback import GithubResultDeliveryError, deliver_github_issue_result
from .github_callback_outbox import GithubCallbackOutbox


# Protocol handshake and lightweight liveness receipts. The connector token remains
# the sole authentication secret; the response never echoes it or the lease token.
class HandshakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    protocol_version: Literal["1.0"] = "1.0"
    client_version: str = Field(default="unknown", min_length=1, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=20)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    heartbeat_id: str = Field(min_length=8, max_length=128)
    status: Literal["idle", "busy", "degraded"]
    current_task_id: str | None = Field(default=None, max_length=128)


def _server_time() -> str:
    return datetime.now(timezone.utc).isoformat()



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

    def runtime():
        hub = get_hub()
        settings = hub.settings
        expected = settings.workbuddy_connector_token
        if not expected or len(expected) < 32:
            raise HTTPException(status_code=503, detail="WorkBuddy connector 尚未配置有效的独立令牌（至少 32 个字符）")
        return hub, settings

    def authenticate(token: str | None, settings: Any) -> None:
        if not token:
            raise HTTPException(status_code=401, detail="缺少 X-WorkBuddy-Connector-Token")
        if not hmac.compare_digest(token, settings.workbuddy_connector_token):
            raise HTTPException(status_code=403, detail="WorkBuddy connector token 无效")

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

    def lease_args(body: LeaseRequest, settings: Any) -> tuple[str, str, int, int]:
        return settings.workbuddy_connector_id, body.lease_token, body.lease_version, settings.workbuddy_connector_lease_seconds

    @router.post("/claim")
    async def claim(
        body: ClaimRequest | None = None,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        authenticate(connector_token, settings)
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
    async def start(
        task_id: str,
        body: LeaseRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        authenticate(connector_token, settings)
        connector_id, token, version, _ = lease_args(body, settings)
        try:
            task = store_for(hub).start_pull_task(task_id, connector_id, token, version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        hub.registry.bus.publish(task_id, task)
        return task_to_wire(task, include_internal=True)

    @router.post("/tasks/{task_id}/renew")
    async def renew(
        task_id: str,
        body: LeaseRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        authenticate(connector_token, settings)
        connector_id, token, version, seconds = lease_args(body, settings)
        try:
            expires = store_for(hub).renew_pull_lease(task_id, connector_id, token, version, seconds)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"lease_expires_at": expires}

    @router.post("/tasks/{task_id}/progress")
    async def progress(
        task_id: str,
        body: ProgressRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        authenticate(connector_token, settings)
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
    async def result(
        task_id: str,
        body: ResultRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        hub, settings = runtime()
        authenticate(connector_token, settings)
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
    async def callback_status(task_id: str, connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token")) -> dict[str, Any]:
        _, settings = runtime()
        authenticate(connector_token, settings)
        status = outbox_for(settings).status(task_id)
        if status is None:
            raise HTTPException(status_code=404, detail="回调任务不存在")
        return status

    @router.post("/callbacks/{task_id}/retry")
    async def retry_callback(task_id: str, connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token")) -> dict[str, Any]:
        _, settings = runtime()
        authenticate(connector_token, settings)
        if not outbox_for(settings).retry(task_id):
            raise HTTPException(status_code=409, detail="回调不存在或当前状态不可人工重试")
        return {"task_id": task_id, "status": "pending"}


    @router.post("/handshake")
    async def handshake(
        body: HandshakeRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        authenticate(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        configured = sorted(
            item.strip() for item in settings.workbuddy_connector_allowed_agents.split(",")
            if item.strip()
        )
        if not configured:
            raise HTTPException(status_code=503, detail="WorkBuddy connector agent allowlist is not configured")
        return {
            "accepted": True,
            "connector_id": settings.workbuddy_connector_id,
            "protocol_version": "1.0",
            "client_version": body.client_version,
            "server_time": _server_time(),
            "heartbeat_interval_seconds": 30,
            "lease_seconds": settings.workbuddy_connector_lease_seconds,
            "allowed_agent_ids": configured,
            "capabilities": sorted(
                set(body.capabilities).intersection(
                    {"claim", "start", "renew", "progress", "result", "heartbeat"}
                )
            ),
        }

    @router.post("/heartbeat")
    async def heartbeat(
        body: HeartbeatRequest,
        connector_token: str | None = Header(default=None, alias="X-WorkBuddy-Connector-Token"),
    ) -> dict[str, Any]:
        _, settings = runtime()
        authenticate(connector_token, settings)
        if not hmac.compare_digest(body.connector_id, settings.workbuddy_connector_id):
            raise HTTPException(status_code=403, detail="WorkBuddy connector identity mismatch")
        return {
            "accepted": True,
            "connector_id": settings.workbuddy_connector_id,
            "receipt_id": body.heartbeat_id,
            "status": body.status,
            "current_task_id": body.current_task_id,
            "server_time": _server_time(),
            "next_heartbeat_seconds": 30,
        }

    return router
