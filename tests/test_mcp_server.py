# -*- coding: utf-8 -*-
"""MCP stdio server 的协议回归。

为什么单独测：MCP over stdio 是最容易「看起来对、连上就崩」的一层——
stdout 混进一行 print、异常分支里再抛一次、通知被当成请求回包，
都能让客户端直接断连，而这些问题在普通单测里一个都暴露不出来。

所以这里用 subprocess **真拉起** server 走一遍对话，而不是直接调函数。

已抓到的真实缺陷（勿删其对应用例）：
- 收到「合法 JSON 但不是对象」（裸字符串 / 数组）时，`msg.get(...)` 会
  AttributeError，**连异常分支里的那次 `msg.get("id")` 也会炸**，
  于是整个进程静默退出、后续所有请求全部丢失。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "mcp_server.py"

EXPECTED_TOOLS = {
    "a2a_agents",
    "a2a_route",
    "a2a_delegate",
    "a2a_collab",
    "a2a_task",
    "a2a_social",
    "a2a_social_act",
    "a2a_social_init",
    "a2a_poll",
    "a2a_submit",
}


def talk(messages: list) -> tuple[list[dict], str]:
    """str 元素按原文发送（构造语法错误的真脏 JSON），dict 才序列化。"""
    payload = "\n".join(
        m if isinstance(m, str) else json.dumps(m, ensure_ascii=False) for m in messages
    ) + "\n"
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=str(REPO_ROOT),
        env={**os.environ, "CODEBUDDY_SAFE_DELETE_ENABLED": "0"},
    )
    return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()], proc.stderr


@pytest.fixture(scope="module")
def handshake() -> tuple[list[dict], str]:
    out, err = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # 通知
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    return out, err


def _by_id(out: list[dict], rid: int) -> dict:
    return next((r for r in out if r.get("id") == rid), {})


def test_notification_is_not_answered(handshake):
    """通知（无 id）绝不能回包，否则客户端会把它当错序响应处理。"""
    out, _ = handshake
    assert len(out) == 2, f"3 条消息里 1 条是通知，应只回 2 条，实际 {len(out)}"


def test_initialize_returns_server_info(handshake):
    out, _ = handshake
    res = _by_id(out, 1).get("result", {})
    assert res.get("serverInfo", {}).get("name") == "a2a-hub"
    assert res.get("protocolVersion") == "2024-11-05"


def test_server_info_version_has_single_source(handshake):
    """握手回包的版本号必须来自 ``a2a_hub.__version__``。

    这个断言防的是一次真实事故：版本号曾被手写在 4 个文件里，结果 README
    已经 v0.6.0、pyproject 还停在 0.5.0。只断言「有版本号」不够——必须断言
    它**等于那个唯一来源**，否则下一次漂移照样漏网。
    """
    from a2a_hub import __version__

    out, _ = handshake
    res = _by_id(out, 1).get("result", {})
    version = res.get("serverInfo", {}).get("version")
    assert version, "握手回包缺 version"
    assert version == __version__, (
        f"握手版本 {version!r} 与包版本 {__version__!r} 不一致 —— 有人又手写了"
    )
    assert version != "unknown", "版本解析失败被静默吞掉了"


def test_tools_list_matches_registered(handshake):
    out, _ = handshake
    tools = _by_id(out, 2).get("result", {}).get("tools", [])
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.get("description"), f"{t['name']} 缺 description"
        assert t.get("inputSchema"), f"{t['name']} 缺 inputSchema"


def test_stdout_has_no_log_noise(handshake):
    """stdout 里解析出的行数 == 请求数，说明日志没混进协议流。"""
    out, _ = handshake
    assert len(out) == 2


def test_workbuddy_poll_and_submit_tools():
    """验证 ChatGPT ↔ WorkBuddy 双向通信闭环所需的 a2a_poll 和 a2a_submit 工具。"""
    out, _ = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "a2a_poll", "arguments": {"worker_id": "test_worker"}}},
    ])
    
    # 验证 poll 回包结构
    res = _by_id(out, 2).get("result", {})
    assert "content" in res
    
    # 接着测试 a2a_submit 工具
    out2, _ = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "a2a_submit", "arguments": {"task_id": "non_existent", "result": "ok", "worker_id": "test_worker"}}},
    ])
    res2 = _by_id(out2, 2).get("result", {})
    assert "content" in res2