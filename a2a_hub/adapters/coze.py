"""扣子 Coze 适配器（OpenAPI v3，SSE 流式）。

文档：https://www.coze.cn/open/docs/developer_guides/coze_api_overview

把 Coze Bot 当作一个可被 A2A 任务调用的 agent：Hub 收到 A2A Task 后，
把消息投给 Coze 的 /v3/chat，再把 Coze 的 SSE 事件翻译回 A2A 事件。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..models import TaskEvent
from .base import AdapterError, BaseAdapter, TaskContext


class CozeAdapter(BaseAdapter):
    type = "coze"
    streaming = True

    def __init__(self, spec, hub_settings=None) -> None:
        super().__init__(spec, hub_settings)
        cfg = spec.config or {}
        self.base_url = str(cfg.get("base_url", "https://api.coze.cn")).rstrip("/")
        self.api_token = cfg.get("api_token", "") or ""
        self.bot_id = cfg.get("bot_id", "") or ""
        self.user_id = cfg.get("user_id", "a2a-hub")
        self.timeout = float(cfg.get("timeout", 180))
        self.extra_params = cfg.get("extra_params", {}) or {}
        # 会话续接：把 A2A contextId 映射到 Coze conversation_id
        self._conversations: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    async def health(self) -> dict[str, Any]:
        if not self.api_token or not self.bot_id:
            missing = [k for k, v in (("api_token", self.api_token), ("bot_id", self.bot_id)) if not v]
            return {"status": "unavailable", "detail": "缺少 " + "/".join(missing)}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{self.base_url}/v1/space/bots",
                    params={"page_num": 1, "page_size": 1},
                    headers=self._headers(),
                )
            if r.status_code == 200:
                return {"status": "healthy", "detail": "Coze token 有效"}
            return {"status": "degraded", "detail": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        if not self.api_token or not self.bot_id:
            raise AdapterError("Coze 适配器未配置 api_token / bot_id")

        body: dict[str, Any] = {
            "bot_id": self.bot_id,
            "user_id": self.user_id,
            "stream": True,
            "auto_save_history": True,
            "additional_messages": [
                {"role": "user", "content": ctx.prompt, "content_type": "text"}
            ],
        }
        if ctx.task.contextId in self._conversations:
            body["conversation_id"] = self._conversations[ctx.task.contextId]
        body.update(self.extra_params)

        url = f"{self.base_url}/v3/chat"
        first = True
        got = False

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "replace")[:600]
                        raise AdapterError(f"Coze 返回 HTTP {resp.status_code}: {raw}")

                    async for event_name, data in _iter_sse(resp):
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("code") not in (None, 0):
                            raise AdapterError(
                                f"Coze 错误 {obj.get('code')}: {obj.get('msg')}"
                            )

                        # 记住 conversation_id 以维持多轮上下文
                        if obj.get("conversation_id"):
                            self._conversations[ctx.task.contextId] = obj["conversation_id"]

                        if event_name == "conversation.message.delta":
                            chunk = obj.get("content") or ""
                            if chunk:
                                got = True
                                yield ctx.artifact(chunk, name="output", append=not first)
                                first = False
                        elif event_name == "conversation.message.completed":
                            if obj.get("type") == "follow_up":
                                continue
                            if not got and obj.get("content"):
                                got = True
                                yield ctx.artifact(obj["content"], name="output")
                        elif event_name in ("conversation.chat.failed", "error"):
                            raise AdapterError(f"Coze 会话失败：{obj.get('msg') or obj}")
        except httpx.HTTPError as exc:
            raise AdapterError(f"网络请求失败：{type(exc).__name__}: {exc}") from exc

        if not got:
            raise AdapterError("Coze 未返回任何内容")
        yield ctx.artifact("", name="output", append=True, last_chunk=True)
        for e in ctx.finish():
            yield e


async def _iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """解析 SSE：产出 (event_name, data) 二元组。"""
    event_name = "message"
    data_lines: list[str] = []
    async for raw in resp.aiter_lines():
        if raw == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = "message", []
            continue
        if raw.startswith("event:"):
            event_name = raw[6:].strip()
        elif raw.startswith("data:"):
            data_lines.append(raw[5:].strip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


__all__ = ["CozeAdapter"]
