"""OpenAI 兼容 HTTP 适配器。

一个适配器覆盖一大票厂商——只要它提供 /chat/completions 兼容接口：
  通义千问 / 千问办公   https://dashscope.aliyuncs.com/compatible-mode/v1
  DeepSeek              https://api.deepseek.com/v1
  Moonshot / Kimi       https://api.moonshot.cn/v1
  智谱 GLM              https://open.bigmodel.cn/api/paas/v4
  百度千帆              https://qianfan.baidubce.com/v2
  火山方舟/豆包          https://ark.cn-beijing.volces.com/api/v3
  本地 Ollama           http://localhost:11434/v1
  本地 vLLM / LM Studio http://localhost:8000/v1
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..models import TaskEvent
from .base import AdapterError, BaseAdapter, TaskContext


class OpenAICompatAdapter(BaseAdapter):
    type = "openai_compat"
    streaming = True

    def __init__(self, spec, hub_settings=None) -> None:
        super().__init__(spec, hub_settings)
        cfg = spec.config or {}
        self.base_url = str(cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.model = cfg.get("model", "gpt-4o-mini")
        self.api_key = cfg.get("api_key", "") or ""
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = cfg.get("temperature", None)
        self.max_tokens = cfg.get("max_tokens", None)
        self.extra_headers = cfg.get("headers", {}) or {}
        self.timeout = float(cfg.get("timeout", 180))

    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    def _payload(self, ctx: TaskContext, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        # 带上历史，把 A2A 的 task history 映射成多轮对话
        for m in ctx.task.history:
            if m.messageId == ctx.message.messageId:
                continue
            messages.append(
                {"role": "assistant" if m.role == "agent" else "user", "content": m.text()}
            )
        messages.append({"role": "user", "content": ctx.prompt})

        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": stream}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        return payload

    async def health(self) -> dict[str, Any]:
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            return {"status": "unavailable", "detail": "缺少 api_key"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{self.base_url}/models", headers=self._headers()
                )
            if r.status_code < 500:
                return {"status": "healthy", "detail": f"HTTP {r.status_code}"}
            return {"status": "degraded", "detail": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ #

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        payload = self._payload(ctx, stream=True)

        got_any = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:600]
                        raise AdapterError(f"{url} 返回 HTTP {resp.status_code}: {body}")

                    first = True
                    async for raw in resp.aiter_lines():
                        if not raw or not raw.startswith("data:"):
                            continue
                        chunk = raw[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        delta = self._extract_delta(obj)
                        if delta:
                            got_any = True
                            yield ctx.artifact(delta, name="output", append=not first)
                            first = False
        except httpx.HTTPError as exc:
            raise AdapterError(f"网络请求失败：{type(exc).__name__}: {exc}") from exc

        if not got_any:
            # 部分网关不支持流式，回退到非流式一次性调用
            async for e in self._execute_blocking(ctx, headers):
                yield e
            return

        yield ctx.artifact("", name="output", append=True, last_chunk=True)
        for e in ctx.finish():
            yield e

    async def _execute_blocking(
        self, ctx: TaskContext, headers: dict[str, str]
    ) -> AsyncIterator[TaskEvent]:
        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url, headers=headers, json=self._payload(ctx, stream=False)
                )
        except httpx.HTTPError as exc:
            raise AdapterError(f"网络请求失败：{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            raise AdapterError(
                f"{url} 返回 HTTP {resp.status_code}: {resp.text[:600]}"
            )
        data = resp.json()
        text = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        )
        yield ctx.artifact(text or "(空响应)", name="output")
        for e in ctx.finish():
            yield e

    @staticmethod
    def _extract_delta(obj: dict[str, Any]) -> str:
        choices = obj.get("choices") or []
        if not choices:
            return ""
        c = choices[0]
        delta = c.get("delta") or {}
        # 兼容 reasoning 模型：思考内容也一并回传，避免"看起来卡住"
        return delta.get("content") or ""


__all__ = ["OpenAICompatAdapter"]
