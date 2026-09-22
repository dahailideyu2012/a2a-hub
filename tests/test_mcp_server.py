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


def test_agents_and_route_work():
    out, _ = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "a2a_agents", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "a2a_route", "arguments": {"query": "调试这段脚本为什么会超时"}}},
    ])

    def text_of(rid: int) -> str:
        return (_by_id(out, rid).get("result", {}) or {}).get("content", [{}])[0].get("text", "")

    agents = text_of(1)
    assert "echo" in agents and "codex" in agents

    route = text_of(2)
    # 第一行是任务，第二行空，第三行才是首选
    assert "codex" in route.splitlines()[2], route


def test_delegate_runs_real_agent():
    """真派一个任务给 echo agent（零依赖、必成功），确认能拿到终态结果。"""
    out, _ = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "a2a_delegate",
                    "arguments": {"text": "ping 连通性自检", "agent": "echo", "timeout": 60}}},
    ])
    text = (_by_id(out, 1).get("result", {}) or {}).get("content", [{}])[0].get("text", "")
    assert "state=completed" in text, text
    assert "ping" in text


def test_social_write_requires_confirm():
    """写闸门：缺 confirm 时必须什么都不改，只回显计划。"""
    out, _ = talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "a2a_social_act",
                    "arguments": {"action": "request", "member": "codex",
                                  "reason": "需要算法能力"}}},
    ])
    text = (_by_id(out, 1).get("result", {}) or {}).get("content", [{}])[0].get("text", "")
    assert "未执行" in text, text
    assert "confirm" in text


def test_malformed_input_does_not_kill_server():
    """脏数据（语法错误 / 非对象）之后，服务必须还能正常应答。"""
    out, _ = talk([
        "这不是 JSON",      # 语法错误
        '"just a string"',  # 合法 JSON，但不是对象
        [1, 2, 3],          # 合法 JSON，但也不是对象
        {"jsonrpc": "2.0", "id": 10, "method": "no/such/method"},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 12, "method": "ping"},
    ])
    ids = [r.get("id") for r in out]
    assert ids == [10, 11, 12], f"三条脏数据不该回包，且服务不能崩：{ids}"
    assert _by_id(out, 10)["error"]["code"] == -32601
    assert _by_id(out, 11)["result"]["isError"] is True
