from __future__ import annotations

import json
import re

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def connection_check_environment(monkeypatch):
    server.HANDSHAKE_READY = False
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "workbuddy-personal")
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "x" * 48)


@pytest.mark.parametrize(
    ("final_presence", "expected_stage", "expected_reason"),
    [
        ({"connector_id": "workbuddy-personal", "online": False}, "final_presence_online", "未确认 online"),
        ({"connector_id": "workbuddy-personal"}, "final_presence_online", "未确认 online"),
        ({"connector_id": "other-connector", "online": True}, "final_presence_identity", "identity"),
        ({"online": True}, "final_presence_identity", "identity"),
        ({"connector_id": "workbuddy-personal", "online": "true"}, "final_presence_online", "未确认 online"),
        ({"connector_id": "workbuddy-personal", "online": 1}, "final_presence_online", "未确认 online"),
    ],
)
def test_connection_check_failure_includes_safe_trace_metadata(
    monkeypatch, final_presence, expected_stage, expected_reason
):
    get_count = 0

    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "heartbeat":
            return {"accepted": True, "receipt_id": "hub-receipt-not-copied-to-error"}
        raise AssertionError(f"unexpected POST {path}")

    def fake_get(path):
        nonlocal get_count
        assert path == "presence"
        get_count += 1
        if get_count == 1:
            return {"connector_id": "workbuddy-personal", "online": True}
        return final_presence

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)
    result = server.call_tool("workbuddy_connection_check", {})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert expected_reason in message
    match = re.search(r"\[connection_check_trace=(\{.*\})\]$", message)
    assert match, message
    trace = json.loads(match.group(1))
    assert trace["check_id"].startswith("conn-")
    assert trace["started_at"].endswith("Z")
    assert trace["failed_stage"] == expected_stage
    assert trace["handshake_observed_at"].endswith("Z")
    assert trace["presence_before_observed_at"].endswith("Z")
    assert trace["heartbeat_observed_at"].endswith("Z")
    assert trace["presence_after_observed_at"].endswith("Z")
    assert "hub-receipt" not in message
    assert "x" * 48 not in message
    assert "lease_token" not in message
    assert get_count == 2


def test_connection_check_heartbeat_rejection_includes_trace_without_secrets(monkeypatch):
    calls = []

    def fake_post(path, payload):
        calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {"accepted": False, "receipt_id": "must-not-appear"}

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", lambda _path: {"online": True, "connector_id": "workbuddy-personal"})
    result = server.call_tool("workbuddy_connection_check", {})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "未确认连接检查心跳" in message
    match = re.search(r"\[connection_check_trace=(\{.*\})\]$", message)
    assert match
    trace = json.loads(match.group(1))
    assert trace["failed_stage"] == "heartbeat_acceptance"
    assert trace["check_id"].startswith("conn-")
    assert "must-not-appear" not in message
    assert calls == ["handshake", "heartbeat"]
