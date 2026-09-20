"""远程 A2A 客户端适配器 —— 真正的 agent-to-agent 级联。

本 Hub 可以把自己未直接支持的 A2A agent（比如别人用 LangChain、ADK、
Semantic Kernel 搭的服务）挂进来自动借力：拉取对方的 Agent Card，
把本地 Task 转成对端的 JSON-RPC 调用，再把对端事件流翻译回来。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..models import TaskEvent, TaskState
from .base import AdapterError, BaseAdapter, TaskContext


class RemoteA2AAdapter(BaseAdapter):
    type = "remote_a2a"
    streaming = True

    def __init__(self, spec, hub_settings=None) -> None:
        super().__init__(spec, hub_settings)
        cfg = spec.config or {}
        self.endpoint = str(cfg.get("url") or spec.url or "").rstrip("/")
        self.card_url = cfg.get("card_url") or (
            f"{self.endpoint}/.well-known/agent.json" if self.endpoint else ""
        )
        self.discover = bool(cfg.get("discover", True))
        self.headers = cfg.get("headers", {}) or {}
        self.timeout = float(cfg.get("timeout", 300))
        self.prefer_stream = bool(cfg.get("stream", True))
        self._remote_card: dict[str, Any] = {}

    # ------------------------------------------------------------------ #

    async def fetch_card(self) -> dict[str, Any]:
        if self._remote_card:
            return self._remote_card
        if not self.card_url:
            raise AdapterError("未配置远程 A2A 端点 url")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(self.card_url, headers=self.headers)
            r.raise_for_status()
            self._remote_card = r.json()
        except httpx.HTTPStatusError:
            # 部分实现把卡片放在 /.well-known/agent-card.json
            alt = self.card_url.replace("agent.json", "agent-card.json")
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(alt, headers=self.headers)
            r.raise_for_status()
            self._remote_card = r.json()
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"拉取远程 Agent Card 失败：{exc}") from exc
        return self._remote_card

    async def health(self) -> dict[str, Any]:
        if not self.endpoint:
            return {"status": "unavailable", "detail": "未配置 url"}
        try:
            card = await self.fetch_card()
            return {
                "status": "healthy",
                "detail": f"远端 agent: {card.get('name', '?')} v{card.get('version', '?')}",
            }
        except AdapterError as exc:
            return {"status": "unavailable", "detail": str(exc)}

    def card(self, base_url: str) -> dict[str, Any]:
        """本地可见的卡片：补上远端技能，方便被协同编排路由。"""
        base = super().card(base_url)
        remote_skills = self._remote_card.get("skills")
        if remote_skills:
            base["skills"] = remote_skills
        if self._remote_card.get("description"):
            base["description"] = self._remote_card["description"]
        base["metadata"]["remoteCard"] = self._remote_card
        return base

    # ------------------------------------------------------------------ #

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        if not self.endpoint:
            raise AdapterError("远程 A2A 适配器未配置 url")

        rpc_id = f"req-{ctx.task.id}"
        params = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": ctx.prompt}],
                "messageId": ctx.message.messageId,
                "contextId": ctx.task.contextId,
                "kind": "message",
            },
            "metadata": {"hubTaskId": ctx.task.id, "callerAgent": "a2a-hub"},
        }
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": None, "params": params}

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            **self.headers,
        }

        got = False
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            method = "message/stream" if self.prefer_stream else "message/send"
            payload["method"] = method
            try:
                if self.prefer_stream:
                    async for ev in self._stream_call(client, headers, payload, ctx):
                        got = True
                        yield ev
                else:
                    resp = await client.post(self.endpoint, headers=headers, json=payload)
                    resp.raise_for_status()
                    for ev in self._apply_rpc_result(ctx, resp.json()):
                        got = True
                        yield ev
            except httpx.HTTPStatusError as exc:
                # 对端不支持流式时降级为同步调用
                if self.prefer_stream and exc.response.status_code in (400, 404, 405, 501):
                    payload["method"] = "message/send"
                    resp = await client.post(self.endpoint, headers=headers, json=payload)
                    resp.raise_for_status()
                    for ev in self._apply_rpc_result(ctx, resp.json()):
                        got = True
                        yield ev
                else:
                    raise AdapterError(f"远程调用失败：HTTP {exc.response.status_code}") from exc
            except httpx.HTTPError as exc:
                raise AdapterError(f"远程调用网络错误：{type(exc).__name__}: {exc}") from exc

        if not got:
            raise AdapterError("远程 agent 未返回任何内容")
        for e in ctx.finish():
            yield e

    # ------------------------------------------------------------------ #

    async def _stream_call(
        self, client: httpx.AsyncClient, headers: dict[str, str], payload: dict, ctx: TaskContext
    ) -> AsyncIterator[TaskEvent]:
        async with client.stream("POST", self.endpoint, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise httpx.HTTPStatusError(
                    raw, request=resp.request, response=resp
                )
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                body = await resp.aread()
                for ev in self._apply_rpc_result(ctx, json.loads(body or b"{}")):
                    yield ev
                return

            first = True
            async for raw in resp.aiter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for ev in self._translate(obj, ctx, first):
                    first = False
                    yield ev

    def _apply_rpc_result(self, ctx: TaskContext, envelope: dict) -> list[TaskEvent]:
        if envelope.get("error"):
            err = envelope["error"]
            raise AdapterError(f"对端 JSON-RPC 错误 {err.get('code')}: {err.get('message')}")
        return self._translate(envelope.get("result") or {}, ctx, True)

    def _translate(self, obj: dict[str, Any], ctx: TaskContext, first: bool) -> list[TaskEvent]:
        """把对端 A2A 事件映射成本地事件。"""
        if not obj:
            return []
        kind = obj.get("kind")
        out: list[TaskEvent] = []

        if kind == "artifact-update":
            art = obj.get("artifact") or {}
            text = _artifact_text(art)
            if text:
                out.append(ctx.artifact(text, name="output", append=not first))
            if obj.get("lastChunk"):
                out.append(ctx.artifact("", name="output", append=True, last_chunk=True))
            return out

        if kind == "status-update":
            state = (obj.get("status") or {}).get("state", "working")
            msg = ((obj.get("status") or {}).get("message") or {})
            text = _parts_text(msg.get("parts") or [])
            try:
                st = TaskState(state)
            except ValueError:
                st = TaskState.WORKING
            out.append(ctx.status(st, text or None, final=bool(obj.get("final"))))
            return out

        if kind == "task":
            for art in obj.get("artifacts") or []:
                text = _artifact_text(art)
                if text:
                    out.append(ctx.artifact(text, name="output", append=False))
            state = (obj.get("status") or {}).get("state", "completed")
            try:
                st = TaskState(state)
            except ValueError:
                st = TaskState.COMPLETED
            out.append(ctx.status(st, final=True))
            return out

        if kind == "message":
            text = _parts_text(obj.get("parts") or [])
            if text:
                out.append(ctx.artifact(text, name="output"))
            return out

        # 裸 result（无 kind）兜底
        text = json.dumps(obj, ensure_ascii=False)
        out.append(ctx.artifact(text, name="output"))
        return out


def _artifact_text(artifact: dict[str, Any]) -> str:
    return _parts_text(artifact.get("parts") or [])


def _parts_text(parts: list[dict[str, Any]]) -> str:
    buf: list[str] = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if p.get("kind") == "text" or "text" in p:
            buf.append(str(p.get("text", "")))
        elif p.get("kind") == "data":
            buf.append(json.dumps(p.get("data") or {}, ensure_ascii=False))
    return "\n".join(buf)


__all__ = ["RemoteA2AAdapter"]
