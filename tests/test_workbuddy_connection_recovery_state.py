from __future__ import annotations

import json
import time

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def recovery_state(monkeypatch):
    server.HANDSHAKE_READY = False
    server.HANDSHAKE_CACHE_KEY = None
    server.LAST_RECOVERY_SNAPSHOT = None
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "workbuddy-personal")
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "x" * 48)


def _diagnostics(snapshot_id="a" * 64):
    return {
        "connector_id": "workbuddy-personal",
        "diagnostics_version": 4,
        "recovery_snapshot_id": snapshot_id,
        "connection_state": "offline",
        "recovery": {"action": "reconnect", "automatic_actions_enabled": False},
        "operator_decision": None,
    }


def test_t169_presence_status_rejects_non_object_without_attribute_error(monkeypatch):
    monkeypatch.setattr(server, "_ensure_handshake", lambda: {"accepted": True})
    monkeypatch.setattr(server, "_get", lambda _path: ["not-an-object"])
    result = server.call_tool("workbuddy_connector_status", {})
    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "must be an object" in message
    assert "AttributeError" not in message


@pytest.mark.parametrize(
    ("status", "task_id"),
    [("idle", "task-1"), ("busy", None), ("busy", ""), ("busy", "  ")],
)
def test_t170_heartbeat_rejects_inconsistent_status_task_pair_before_network(
    monkeypatch, status, task_id
):
    calls = []
    monkeypatch.setattr(
        server, "_post", lambda path, payload: calls.append((path, payload)) or {}
    )
    result = server.call_tool(
        "workbuddy_heartbeat",
        {"status": status, "current_task_id": task_id},
    )
    assert result["isError"] is True
    assert calls == []


def test_t170_shared_heartbeat_path_invalidates_handshake_after_auth_rejection(monkeypatch):
    server.HANDSHAKE_READY = True
    server.HANDSHAKE_CACHE_KEY = server._handshake_cache_key()
    monkeypatch.setattr(server, "_ensure_handshake", lambda: {"accepted": True})
    monkeypatch.setattr(
        server, "_post",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Hub HTTP 401: denied")),
    )
    result = server.call_tool(
        "workbuddy_heartbeat",
        {"status": "busy", "current_task_id": "task-1"},
    )
    assert result["isError"] is True
    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None


@pytest.mark.parametrize(
    "bad_recovery",
    [None, [], {"action": None, "automatic_actions_enabled": False},
     {"action": "reconnect", "automatic_actions_enabled": True}],
)
def test_t171_recovery_diagnostics_requires_safe_structured_recovery_contract(
    monkeypatch, bad_recovery
):
    payload = _diagnostics()
    payload["recovery"] = bad_recovery
    monkeypatch.setattr(server, "_get", lambda _path: payload)
    result = server.call_tool("workbuddy_recovery_diagnostics", {})
    assert result["isError"] is True
    assert server.LAST_RECOVERY_SNAPSHOT is None


def test_t171_new_diagnostics_snapshot_replaces_previous_snapshot(monkeypatch):
    snapshots = iter(["b" * 64, "c" * 64])
    monkeypatch.setattr(
        server, "_get",
        lambda _path: _diagnostics(next(snapshots)),
    )
    assert server.tool_recovery_diagnostics({})["isError"] is False
    assert server.LAST_RECOVERY_SNAPSHOT[1] == "b" * 64
    assert server.tool_recovery_diagnostics({})["isError"] is False
    assert server.LAST_RECOVERY_SNAPSHOT[1] == "c" * 64


def test_t172_recovery_decision_requires_matching_fresh_read_snapshot_before_post(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "_post", lambda path, payload: calls.append((path, payload)) or {}
    )
    result = server.call_tool("workbuddy_record_recovery_decision", {
        "snapshot_id": "d" * 64,
        "disposition": "acknowledged",
    })
    assert result["isError"] is True
    assert "必须先读取当前" in result["content"][0]["text"]
    assert calls == []


def test_t172_recovery_decision_consumes_snapshot_before_write_and_requires_reread(monkeypatch):
    snapshot_id = "e" * 64
    monkeypatch.setattr(server, "_get", lambda _path: _diagnostics(snapshot_id))
    assert server.tool_recovery_diagnostics({})["isError"] is False
    calls = []

    def fail_once(path, payload):
        calls.append((path, payload.copy()))
        raise RuntimeError("temporary write failure")

    monkeypatch.setattr(server, "_post", fail_once)
    first = server.call_tool("workbuddy_record_recovery_decision", {
        "snapshot_id": snapshot_id,
        "disposition": "deferred",
    })
    second = server.call_tool("workbuddy_record_recovery_decision", {
        "snapshot_id": snapshot_id,
        "disposition": "deferred",
    })
    assert first["isError"] is True
    assert second["isError"] is True
    assert len(calls) == 1
    assert server.LAST_RECOVERY_SNAPSHOT is None


def test_t172_handshake_invalidation_clears_pending_recovery_snapshot(monkeypatch):
    snapshot_id = "f" * 64
    monkeypatch.setattr(server, "_get", lambda _path: _diagnostics(snapshot_id))
    assert server.tool_recovery_diagnostics({})["isError"] is False
    assert server.LAST_RECOVERY_SNAPSHOT is not None
    server._invalidate_handshake()
    assert server.LAST_RECOVERY_SNAPSHOT is None

def test_t172_recovery_decision_rejects_snapshot_after_connector_token_changes(
    monkeypatch,
):
    snapshot_id = "9" * 64
    monkeypatch.setattr(server, "_get", lambda _path: _diagnostics(snapshot_id))
    assert server.tool_recovery_diagnostics({})["isError"] is False

    calls = []
    monkeypatch.setattr(
        server, "_post", lambda path, payload: calls.append((path, payload)) or {}
    )
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "y" * 48)

    result = server.call_tool(
        "workbuddy_record_recovery_decision",
        {"snapshot_id": snapshot_id, "disposition": "acknowledged"},
    )

    assert result["isError"] is True
    assert "必须先读取当前" in result["content"][0]["text"]
    assert calls == []
