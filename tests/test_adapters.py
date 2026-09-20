"""适配器测试 —— 重点是 CLI 解析器（异构输出归一化）与配置装载。"""

from __future__ import annotations

import json

import pytest

from a2a_hub.adapters import ADAPTER_TYPES, build_adapter
from a2a_hub.adapters.base import TaskContext, split_command
from a2a_hub.adapters.cli_agents import (
    ClaudeParser,
    CodexParser,
    JsonlParser,
    TextParser,
)
from a2a_hub.adapters.echo import EchoAdapter
from a2a_hub.config import AgentSpec, expand_env
from a2a_hub.models import Message, Task, TaskState


# --------------------------------------------------------------------------- #
# 配置展开
# --------------------------------------------------------------------------- #


def test_expand_env_placeholders(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret")
    data = {
        "a": "${MY_KEY}",
        "b": "Bearer ${MY_KEY}",
        "c": "${MISSING}",
        "d": "${MISSING:-fallback}",
        "e": ["${MY_KEY}", {"f": "${MISSING:-xx}"}],
    }
    out = expand_env(data)
    assert out["a"] == "secret"
    assert out["b"] == "Bearer secret"
    assert out["c"] == ""
    assert out["d"] == "fallback"
    assert out["e"] == ["secret", {"f": "xx"}]


def test_split_command_handles_quotes():
    tokens = split_command('claude -p {prompt} --output-format stream-json')
    assert tokens[0] == "claude"
    assert "{prompt}" in tokens


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def test_text_parser_passes_lines_through():
    p = TextParser()
    assert p.feed("hello") == ["hello"]
    assert p.feed("") == []


def test_jsonl_parser_extracts_by_path_and_dedupes():
    p = JsonlParser(text_paths=["content"])
    assert p.feed(json.dumps({"content": "你好"})) == ["你好"]
    # 累计前缀形态：只发新增部分
    assert p.feed(json.dumps({"content": "你好世界"})) == ["世界"]
    assert p.feed(json.dumps({"content": "你好世界"})) == []


def test_jsonl_parser_ignores_non_json_lines():
    p = JsonlParser(text_paths=["content"])
    assert p.feed("not json") == []
    assert p.feed("{broken") == []


def test_jsonl_parser_recursive_fallback():
    p = JsonlParser()
    line = json.dumps({"payload": {"nested": {"text": "深处的内容"}}})
    assert p.feed(line) == ["深处的内容"]


def test_claude_parser_stream_json():
    p = ClaudeParser()
    line1 = json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "text", "text": "正在"}]},
    })
    line2 = json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "text", "text": "正在分析"}]},
    })
    assert p.feed(line1) == ["正在"]
    assert p.feed(line2) == ["分析"]

    result = json.dumps({"type": "result", "subtype": "success", "result": "最终结论"})
    # 已有增量产出时 result 不重复发送
    assert p.feed(result) == []


def test_claude_parser_uses_result_when_no_deltas():
    p = ClaudeParser()
    result = json.dumps({"type": "result", "subtype": "success", "result": "只有结果"})
    assert p.feed(result) == ["只有结果"]


def test_codex_parser_variants():
    p = CodexParser()
    l1 = json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "第一步"}})
    l2 = json.dumps({"msg": {"type": "agent_message", "message": "第二步"}})
    assert p.feed(l1) == ["第一步"]
    assert p.feed(l2) == ["第二步"]


def test_codex_parser_ignores_tool_noise():
    p = CodexParser()
    assert p.feed(json.dumps({"type": "turn.started"})) == []


# --------------------------------------------------------------------------- #
# 适配器构建 / 卡片
# --------------------------------------------------------------------------- #


def test_all_declared_types_are_buildable():
    for name, cls in ADAPTER_TYPES.items():
        spec = AgentSpec(id=f"x-{name}", name=name, type=name)
        adapter = cls(spec)
        assert adapter.type == name or adapter.type == cls.type


def test_build_adapter_rejects_unknown_type():
    spec = AgentSpec(id="bad", name="bad", type="does-not-exist")
    with pytest.raises(Exception) as exc:
        build_adapter(spec)
    assert "未知适配器类型" in str(exc.value)


def test_agent_card_contains_protocol_essentials():
    spec = AgentSpec(
        id="demo",
        name="Demo",
        type="echo",
        description="desc",
        skills=[{"id": "s1", "name": "技能一", "tags": ["t"]}],
    )
    card = EchoAdapter(spec).card("http://hub.local")
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"] == "http://hub.local/agents/demo/"
    assert card["capabilities"]["streaming"] is True
    assert card["skills"][0]["id"] == "s1"
    assert card["metadata"]["adapterType"] == "echo"


def test_skill_defaults_are_filled():
    spec = AgentSpec(id="d", name="D", type="echo", skills=[{"id": "only-id"}])
    skills = EchoAdapter(spec).skills
    assert skills[0].name == "only-id"
    assert skills[0].inputModes == ["text/plain"]


# --------------------------------------------------------------------------- #
# CLI argv 构造
# --------------------------------------------------------------------------- #


def test_cli_argv_places_prompt_as_single_token():
    from a2a_hub.adapters.cli_agents import CliAgentAdapter

    spec = AgentSpec(
        id="c", name="C", type="cli", config={"command": "mytool -p {prompt}"}
    )
    adapter = CliAgentAdapter(spec)
    argv, stdin = adapter.build_argv("rm -rf /; echo pwned")
    assert argv[0].endswith("mytool") or argv[0] == "mytool"
    # prompt 必须是独立 argv 元素，不能被 shell 解释
    assert "rm -rf /; echo pwned" in argv
    assert stdin is None


def test_cli_argv_stdin_mode_when_no_placeholder():
    from a2a_hub.adapters.cli_agents import CliAgentAdapter

    spec = AgentSpec(id="c2", name="C2", type="cli", config={"command": "mytool --read-stdin"})
    adapter = CliAgentAdapter(spec)
    argv, stdin = adapter.build_argv("喂进去")
    assert stdin == "喂进去"
    assert argv == ["mytool", "--read-stdin"]


# --------------------------------------------------------------------------- #
# 端到端：echo 适配器跑出完整 A2A 事件流
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_echo_adapter_emits_full_event_lifecycle():
    spec = AgentSpec(id="e", name="E", type="echo", config={"prefix": "[E]", "emit_data_part": True})
    adapter = EchoAdapter(spec)
    task = Task()
    msg = Message.user("你好")
    task.history.append(msg)
    ctx = TaskContext(task=task, message=msg, adapter=adapter)

    kinds: list[str] = []
    async for event in adapter.run(ctx):
        kinds.append(event.kind)

    assert "status-update" in kinds
    assert "artifact-update" in kinds
    assert task.status.state == TaskState.COMPLETED
    assert "[E] 你好" in task.final_text()
    assert any(a.name == "meta" for a in task.artifacts)


@pytest.mark.asyncio
async def test_echo_adapter_fail_injection_maps_to_failed_state():
    spec = AgentSpec(id="e2", name="E2", type="echo", config={"fail_on": "BOOM"})
    adapter = EchoAdapter(spec)
    task = Task()
    msg = Message.user("please BOOM now")
    ctx = TaskContext(task=task, message=msg, adapter=adapter)

    async for _ in adapter.run(ctx):
        pass
    assert task.status.state == TaskState.FAILED
    assert "BOOM" in (task.status.message.text() if task.status.message else "")


@pytest.mark.asyncio
async def test_echo_adapter_input_required_path():
    spec = AgentSpec(id="e3", name="E3", type="echo", config={"input_required_keyword": "need-info"})
    adapter = EchoAdapter(spec)
    task = Task()
    msg = Message.user("need-info please")
    ctx = TaskContext(task=task, message=msg, adapter=adapter)

    async for _ in adapter.run(ctx):
        pass
    assert task.status.state == TaskState.INPUT_REQUIRED


@pytest.mark.asyncio
async def test_static_adapter_returns_configured_text():
    spec = AgentSpec(id="s", name="S", type="static", config={"response": "固定文本"})
    adapter = build_adapter(spec)
    task = Task()
    msg = Message.user("任何输入")
    ctx = TaskContext(task=task, message=msg, adapter=adapter)
    async for _ in adapter.run(ctx):
        pass
    assert task.final_text() == "固定文本"
