"""适配器包 —— 类型注册表与工厂。

新增一个 agent 生态，只需要：
  1. 写一个继承 ``BaseAdapter`` 的类
  2. 在 ``ADAPTER_TYPES`` 里登记 type 名
  3. 在 agents.yaml 里用这个 type 声明 agent
"""

from __future__ import annotations

from typing import Any, Type

from ..config import AgentSpec
from .base import (
    AdapterError,
    BaseAdapter,
    ProcessRegistry,
    TaskContext,
    resolve_executable,
    run_command_stream,
    split_command,
)
from .cli_agents import (
    ClaudeCodeAdapter,
    CliAgentAdapter,
    CodexAdapter,
    GeminiAdapter,
    GenericCliAdapter,
    WorkBuddyAdapter,
)
from .coze import CozeAdapter
from .echo import EchoAdapter, StaticAdapter
from .openai_compat import OpenAICompatAdapter
from .remote_a2a import RemoteA2AAdapter

ADAPTER_TYPES: dict[str, Type[BaseAdapter]] = {
    # 本地 / 演示
    "echo": EchoAdapter,
    "static": StaticAdapter,
    # 主流商业 agent 平台
    "openai_compat": OpenAICompatAdapter,  # 千问办公 / 通义千问 / DeepSeek / Kimi / GLM / Ollama
    "coze": CozeAdapter,  # 扣子
    # 本地 CLI coding agent
    "workbuddy": WorkBuddyAdapter,
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "generic_cli": GenericCliAdapter,
    "cli": CliAgentAdapter,
    # 跨 Hub 级联
    "remote_a2a": RemoteA2AAdapter,
}


def build_adapter(spec: AgentSpec, hub_settings: Any = None) -> BaseAdapter:
    cls = ADAPTER_TYPES.get(spec.type)
    if cls is None:
        raise AdapterError(
            f"未知适配器类型 `{spec.type}`（agent `{spec.id}`）。"
            f"可用类型：{', '.join(sorted(ADAPTER_TYPES))}"
        )
    return cls(spec, hub_settings)


__all__ = [
    "ADAPTER_TYPES",
    "build_adapter",
    "BaseAdapter",
    "TaskContext",
    "AdapterError",
    "ProcessRegistry",
    "run_command_stream",
    "resolve_executable",
    "split_command",
]
