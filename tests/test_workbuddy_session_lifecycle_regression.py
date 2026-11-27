from __future__ import annotations

import time

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def isolated_connector_session(monkeypatch):
    with server.LEASES_LOCK:
        server.LEASES.clear()
    server.HANDSHAKE_READY = False
    server.HANDSHAKE_CACHE_KEY = None
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "workbuddy-personal")
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "x" * 48)
    yield
    with server.LEASES_LOCK:
        server.LEASES.clear()


@pytest.mark.parametrize("status_code", [401, 403, 409, 426])
def test_heartbeat_auth_protocol_conflicts_invalidate_handshake_cache(status_code, monkeypatch):
    calls = []

    def fake_post(path, payload):
        calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        raise RuntimeError(f"Hub HTTP {status_code}: rejected")

    monkeypatch.setattr(server, "_post", fake_post)
    assert server.call_tool("workbuddy_heartbeat", {"status": "idle"})["isError"] is True
    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None


def test_heartbeat_server_error_does_not_invalidate_valid_handshake(monkeypatch):
    status_code = 503
    # Kept as a separate regression: transient server errors do not mean the
    # authenticated connector identity or protocol session is invalid.
    calls = []

    def fake_post(path, payload):
        calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        raise RuntimeError(f"Hub HTTP {status_code}: temporary")

    monkeypatch.setattr(server, "_post", fake_post)
    assert server.tool_heartbeat({"status": "idle"})["isError"] is True
    assert server.HANDSHAKE_READY is True
    assert calls == ["handshake", "heartbeat"]


def test_expired_local_lease_is_removed_before_any_task_mutation(monkeypatch):
    calls = []
    with server.LEASES_LOCK:
        server.LEASES["expired-task"] = {
            "lease_token": "must-not-leak",
            "lease_version": 2,
            "lease_expires_at": time.time() - 1,
        }

    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    for tool_name, args in [
        ("workbuddy_start_task", {"task_id": "expired-task"}),
        ("workbuddy_renew_task", {"task_id": "expired-task"}),
        ("workbuddy_progress_task", {"task_id": "expired-task", "sequence": 1, "message": "x"}),
        ("workbuddy_submit_result", {"task_id": "expired-task", "state": "failed", "output": "x"}),
    ]:
        result = server.call_tool(tool_name, args)
        assert result["isError"] is True
        assert "没有该任务的有效租约" in result["content"][0]["text"]

    assert calls == []
    assert "expired-task" not in server.LEASES


def test_renew_rejects_expired_receipt_without_extending_local_lease(monkeypatch):
    original_expiry = time.time() + 30
    with server.LEASES_LOCK:
        server.LEASES["renew-expired"] = {
            "lease_token": "private-token",
            "lease_version": 3,
            "lease_expires_at": original_expiry,
        }

    monkeypatch.setattr(server, "_post", lambda *_args: {
        "lease_expires_at": time.time() - 1,
    })
    result = server.call_tool("workbuddy_renew_task", {"task_id": "renew-expired"})
    assert result["isError"] is True
    assert server.LEASES["renew-expired"]["lease_expires_at"] == original_expiry


def test_terminal_result_removes_lease_and_prevents_duplicate_result_post(monkeypatch):
    calls = []
    with server.LEASES_LOCK:
        server.LEASES["terminal-task"] = {
            "lease_token": "private-token",
            "lease_version": 4,
            "lease_expires_at": time.time() + 60,
        }

    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append((path, payload.copy())) or {"accepted": True})
    first = server.call_tool("workbuddy_submit_result", {
        "task_id": "terminal-task", "state": "completed", "output": "done",
    })
    second = server.call_tool("workbuddy_submit_result", {
        "task_id": "terminal-task", "state": "completed", "output": "done",
    })

    assert first["isError"] is False
    assert second["isError"] is True
    assert len(calls) == 1
    assert "terminal-task" not in server.LEASES
