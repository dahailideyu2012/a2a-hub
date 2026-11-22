"""Restricted outbound-poll API for the WorkBuddy Personal connector."""
from __future__ import annotations

import asyncio
import hmac
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .ports import PullTaskRepository
from .store import task_to_wire
from .github_result_callback import GithubResultDeliveryError, deliver_github_issue_result


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
            raise HTTPException(status_code=503, detail="WorkBuddy connector agent allowlist is not configured")
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
            try:
                callback_result = await asyncio.to_thread(
                    deliver_github_issue_result,
                    callback, task_id, body.state, body.output,
                    settings.github_relay_api_token, settings.github_relay_allowed_repos,
                )
            except GithubResultDeliveryError:
                # The task result is already durable. Return 502 so WorkBuddy retries
                # the exact same result; marker lookup prevents duplicate Issue comments.
                raise HTTPException(status_code=502, detail="GitHub 结果回传暂时失败；请使用相同 result_key 重试")
        if not replay:
            hub.registry.bus.publish(task_id, task)
            hub.registry.bus.close_task(task_id)
        response = {"task": task_to_wire(task, include_internal=True), "idempotent_replay": replay}
        if callback_result is not None:
            response["github_callback"] = callback_result
        return response

    return router
