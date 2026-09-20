"""数据模型与协议对象测试。"""

from __future__ import annotations

import json

from a2a_hub.models import (
    Artifact,
    DataPart,
    FilePart,
    FileWithUri,
    JsonRpcRequest,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TextPart,
    parse_part,
    part_to_text,
    utc_now,
)


def test_parse_part_accepts_bare_string():
    p = parse_part("你好")
    assert isinstance(p, TextPart)
    assert p.text == "你好"
    assert p.kind == "text"


def test_parse_part_accepts_dicts():
    assert isinstance(parse_part({"kind": "text", "text": "x"}), TextPart)
    assert isinstance(parse_part({"kind": "data", "data": {"a": 1}}), DataPart)
    fp = parse_part({"kind": "file", "file": {"uri": "https://x/y.pdf", "name": "y.pdf"}})
    assert isinstance(fp, FilePart)
    assert isinstance(fp.file, FileWithUri)
    assert fp.file.name == "y.pdf"


def test_parse_part_unknown_dict_degrades_to_data_part():
    p = parse_part({"weird": "shape", "n": 3})
    assert isinstance(p, DataPart)
    assert p.data["n"] == 3


def test_part_to_text_handles_all_kinds():
    assert part_to_text(TextPart(text="abc")) == "abc"
    assert json.loads(part_to_text(DataPart(data={"k": "v"}))) == {"k": "v"}
    assert "y.pdf" in part_to_text(FilePart(file=FileWithUri(uri="http://a/b", name="y.pdf")))


def test_message_helpers_and_text_join():
    m = Message(role="user", parts=[TextPart(text="a"), DataPart(data={"b": 1})])
    assert m.kind == "message"
    assert m.messageId.startswith("msg-")
    assert m.text().splitlines()[0] == "a"
    assert Message.user("hi").text() == "hi"
    assert Message.agent("yo").role == "agent"


def test_task_state_machine_and_artifacts():
    t = Task()
    assert t.status.state == TaskState.SUBMITTED
    assert not t.status.is_terminal

    t.touch(TaskState.WORKING)
    assert t.status.state == TaskState.WORKING

    # 同名 artifact 应追加 parts，而不是新建
    t.add_artifact(Artifact(name="output", parts=[TextPart(text="第一段")]))
    t.add_artifact(Artifact(name="output", parts=[TextPart(text="第二段")]))
    assert len(t.artifacts) == 1
    assert t.final_text() == "第一段\n第二段"

    # 不同名 artifact 各自独立
    t.add_artifact(Artifact(name="meta", parts=[TextPart(text="meta")]))
    assert len(t.artifacts) == 2

    t.touch(TaskState.COMPLETED)
    assert t.status.is_terminal


def test_task_serialization_excludes_internal_fields():
    t = Task(agentId="echo-a")
    data = t.model_dump(mode="json")
    # 内部扩展字段不进 A2A 线上载荷
    assert "agentId" not in data
    assert data["kind"] == "task"
    assert data["status"]["state"] == "submitted"
    assert json.loads(t.model_dump_json())["id"] == t.id


def test_task_artifact_update_event_shape():
    t = Task()
    art = Artifact(name="output", parts=[TextPart(text="x")])
    ev = TaskArtifactUpdateEvent(taskId=t.id, contextId=t.contextId, artifact=art, lastChunk=True)
    dump = ev.model_dump(mode="json")
    assert dump["kind"] == "artifact-update"
    assert dump["lastChunk"] is True
    assert dump["artifact"]["parts"][0]["kind"] == "text"


def test_jsonrpc_request_defaults():
    req = JsonRpcRequest(method="message/send")
    assert req.jsonrpc == "2.0"
    assert req.params == {}
    assert req.id is None


def test_utc_now_is_iso8601_utc():
    ts = utc_now()
    assert "T" in ts and ("+00:00" in ts or ts.endswith("Z"))
