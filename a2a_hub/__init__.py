"""A2A Hub —— 异构 AI Agent 互联互通与多智能体协同网关。

以 Google A2A (Agent-to-Agent) 协议为核心，把 WorkBuddy、千问办公、扣子 Coze、
Codex、Claude Code 等异构 agent 统一封装为符合 A2A 规范的 Agent，
实现「能力发现 -> 任务委派 -> 状态流式回传 -> 结果聚合」的完整协作链路。
"""

# 版本号的**唯一**来源：pyproject.toml / FastAPI / CLI / MCP 握手全部读这里，
# 不各自手写（曾经出现过 README 已 0.6.0、pyproject 还停在 0.5.0 的漂移）。
__version__ = "0.6.0"
__all__ = ["__version__"]
