"""本地回显适配器 —— 零依赖、可离线，用于测试与演示。

它不是"玩具"：它实现了完整的 A2A 事件协议（流式 artifact 增量 +
状态机流转 + 可配置延迟/失败注入），是验证 Hub 全链路正确性的基准 agent。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from ..models import TaskEvent
from .base import AdapterError, BaseAdapter, TaskContext


class EchoAdapter(BaseAdapter):
    type = "echo"
    streaming = True

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "detail": "local echo, no external dependency"}

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        cfg = self.spec.config
        prompt = ctx.prompt

        # 失败注入：便于测试编排器对 failed 任务的处理
        if cfg.get("fail_on") and cfg["fail_on"] in prompt:
            raise AdapterError(f"命中失败注入关键词：{cfg['fail_on']}")

        # 人工输入挂起：测试 input-required 状态
        if cfg.get("input_required_keyword") and cfg["input_required_keyword"] in prompt:
            from ..models import TaskState

            yield ctx.status(TaskState.INPUT_REQUIRED, "需要补充信息才能继续")
            return

        delay = float(cfg.get("chunk_delay", 0.0))
        prefix = cfg.get("prefix", f"[{self.name}]")
        chunk_size = int(cfg.get("chunk_size", 24))

        yield ctx.status("working", "正在处理")  # type: ignore[arg-type]

        body = self._compose(prompt)
        text = f"{prefix} {body}"

        # 分块流式产出，真实还原"边生成边推送"的行为
        if delay > 0 and chunk_size > 0:
            first = True
            for i in range(0, len(text), chunk_size):
                yield ctx.artifact(text[i : i + chunk_size], name="output", append=not first)
                first = False
                await asyncio.sleep(delay)
            yield ctx.artifact("", name="output", append=True, last_chunk=True)
        else:
            yield ctx.artifact(text, name="output")

        # 结构化产出（DataPart 能力演示）
        if cfg.get("emit_data_part"):
            from ..models import Artifact, DataPart

            ctx.task.artifacts.append(
                Artifact(
                    name="meta",
                    parts=[
                        DataPart(
                            data={
                                "agentId": self.id,
                                "adapter": self.type,
                                "promptChars": len(prompt),
                                "echoed": True,
                            }
                        )
                    ],
                )
            )

        for e in ctx.finish():
            yield e

    def _compose(self, prompt: str) -> str:
        style = self.spec.config.get("style", "plain")
        if style == "json":
            return json.dumps(
                {"agent": self.id, "received": prompt, "ok": True}, ensure_ascii=False
            )
        return prompt


class StaticAdapter(BaseAdapter):
    """固定应答适配器 —— 用于把"人"或外部系统接入为 agent 的占位实现。"""

    type = "static"
    streaming = False

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        text = self.spec.config.get("response", "(no static response configured)")
        yield ctx.artifact(text, name="output")
        for e in ctx.finish():
            yield e


__all__ = ["EchoAdapter", "StaticAdapter"]
