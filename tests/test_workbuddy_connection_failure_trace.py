from __future__ import annotations

import json
import re

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def connection_check_environment(monkeypatch):
    server.HANDSHAKE_READY = False
    server.HANDSHAKE_CACHE_KEY = None
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
            return {
                "accepted": True,
                "connector_id": payload["connector_id"],
                "receipt_id": payload["heartbeat_id"],
                "status": payload["status"],
                "current_task_id": payload["current_task_id"],
                "cancel_requested": False,
                "cancel_reason": None,
            }
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


# T165–T168: connection-check stage validation and sanitized failure traces.

def _trace_from_error(result):
    assert result["isError"] is True
    message = result["content"][0]["text"]
    match = re.search(r"\[connection_check_trace=(\{.*\})\]$", message)
    assert match, message
    trace = json.loads(match.group(1))
    assert trace["check_id"].startswith("conn-")
    assert "x" * 48 not in message
    assert "PRIVATE-ERROR-DETAIL" not in message
    return message, trace


def _successful_heartbeat(path, payload):
    if path == "handshake":
        return {"accepted": True, "connector_id": "workbuddy-personal"}
    return {
        "accepted": True,
        "connector_id": payload["connector_id"],
        "receipt_id": payload["heartbeat_id"],
        "status": payload["status"],
        "current_task_id": payload["current_task_id"],
        "cancel_requested": False,
        "cancel_reason": None,
    }


@pytest.mark.parametrize(
    ("initial_presence", "expected_stage"),
    [
        ({"connector_id": "another-connector", "online": True}, "presence_before_identity"),
        ({"connector_id": "workbuddy-personal", "online": "true"}, "presence_before_online"),
        ({"connector_id": "workbuddy-personal"}, "presence_before_online"),
        (["not", "an", "object"], "presence_before_shape"),
    ],
)
def test_t165_initial_presence_is_validated_before_heartbeat(
    monkeypatch, initial_presence, expected_stage
):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or _successful_heartbeat(path, payload))
    monkeypatch.setattr(server, "_get", lambda _path: initial_presence)
    result = server.call_tool("workbuddy_connection_check", {})
    message, trace = _trace_from_error(result)
    assert trace["failed_stage"] == expected_stage
    assert "heartbeat_observed_at" not in trace
    assert calls == ["handshake"]


def test_t166_handshake_transport_failure_has_sanitized_trace(monkeypatch):
    def fail_handshake():
        raise RuntimeError("PRIVATE-ERROR-DETAIL " + "x" * 48)
    monkeypatch.setattr(server, "_ensure_handshake", fail_handshake)
    result = server.call_tool("workbuddy_connection_check", {})
    message, trace = _trace_from_error(result)
    assert trace["failed_stage"] == "handshake"
    assert "handshake_observed_at" not in trace


def test_t167_initial_presence_transport_failure_has_sanitized_trace(monkeypatch):
    monkeypatch.setattr(server, "_post", lambda path, payload: _successful_heartbeat(path, payload))
    monkeypatch.setattr(server, "_get", lambda _path: (_ for _ in ()).throw(RuntimeError("PRIVATE-ERROR-DETAIL")))
    result = server.call_tool("workbuddy_connection_check", {})
    message, trace = _trace_from_error(result)
    assert trace["failed_stage"] == "presence_before"
    assert "handshake_observed_at" in trace
    assert "presence_before_observed_at" not in trace
    assert "heartbeat_observed_at" not in trace


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage"),
    [("heartbeat", "heartbeat_request"), ("final_presence", "presence_after")],
)
def test_t168_late_connection_check_transport_failures_have_stage_trace(
    monkeypatch, failure_stage, expected_stage
):
    get_count = 0
    def fake_post(path, payload):
        if path == "handshake":
            return _successful_heartbeat(path, payload)
        if failure_stage == "heartbeat":
            raise RuntimeError("PRIVATE-ERROR-DETAIL " + "x" * 48)
        return _successful_heartbeat(path, payload)
    def fake_get(_path):
        nonlocal get_count
        get_count += 1
        if failure_stage == "final_presence" and get_count == 2:
            raise RuntimeError("PRIVATE-ERROR-DETAIL")
        return {"connector_id": "workbuddy-personal", "online": True}
    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)
    result = server.call_tool("workbuddy_connection_check", {})
    message, trace = _trace_from_error(result)
    assert trace["failed_stage"] == expected_stage
    if failure_stage == "heartbeat":
        assert "heartbeat_observed_at" not in trace
        assert get_count == 1
    else:
        assert "heartbeat_observed_at" in trace
        assert "presence_after_observed_at" not in trace
        assert get_count == 2
