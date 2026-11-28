from __future__ import annotations

import hashlib
import time

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def clean_leases():
    with server.LEASES_LOCK:
        server.LEASES.clear()
    yield
    with server.LEASES_LOCK:
        server.LEASES.clear()


def seed(task_id="task-validation", expires_in=120):
    with server.LEASES_LOCK:
        server.LEASES[task_id] = {
            "lease_token": "private-lease-material",
            "lease_version": 3,
            "lease_expires_at": time.time() + expires_in,
        }


@pytest.mark.parametrize("bad_task_id", [None, 7, "", "   ", "bad\nkey", "x" * 129])
@pytest.mark.parametrize(
    "tool_name,extra",
    [
        ("workbuddy_start_task", {}),
        ("workbuddy_renew_task", {}),
        ("workbuddy_progress_task", {"sequence": 1, "message": "ok"}),
        ("workbuddy_submit_result", {"state": "completed", "output": "ok"}),
    ],
)
def test_t173_lifecycle_tools_reject_invalid_task_ids_before_network(
    monkeypatch, bad_task_id, tool_name, extra
):
    # Seed even malformed IDs so a missing validator cannot be masked by
    # the downstream "no active lease" guard. The validator must reject first.
    seed(bad_task_id)
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload)) or {},
    )
    result = server.call_tool(tool_name, {"task_id": bad_task_id, **extra})
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("sequence", [None, True, False, 0, -1, 1.5, "1"])
def test_t175_progress_rejects_invalid_sequence_before_network(monkeypatch, sequence):
    seed()
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload)) or {},
    )
    result = server.call_tool(
        "workbuddy_progress_task",
        {"task_id": "task-validation", "sequence": sequence, "message": "checkpoint"},
    )
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("message", [None, "", "x" * 4001, 123])
def test_t175_progress_rejects_invalid_message_before_network(monkeypatch, message):
    seed()
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload)) or {},
    )
    result = server.call_tool(
        "workbuddy_progress_task",
        {"task_id": "task-validation", "sequence": 1, "message": message},
    )
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf"), True, "9999999999", 1, 10 ** 400])
def test_t174_renew_rejects_nonfinite_or_invalid_expiry_without_mutating_lease(
    monkeypatch, expires_at
):
    seed()
    old_expiry = server.LEASES["task-validation"]["lease_expires_at"]
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload)) or {"lease_expires_at": expires_at},
    )
    result = server.call_tool("workbuddy_renew_task", {"task_id": "task-validation"})
    assert result["isError"] is True
    assert len(calls) == 1
    assert server.LEASES["task-validation"]["lease_expires_at"] == old_expiry


@pytest.mark.parametrize(
    "bad_args",
    [
        {"state": "running", "output": "done"},
        {"state": "completed", "output": None},
        {"state": "completed", "output": "x" * 20001},
        {"state": "completed", "output": "done", "result_key": "short"},
        {"state": "completed", "output": "done", "result_key": "x" * 129},
        {"state": "completed", "output": "done", "result_key": 12345678},
    ],
)
def test_t176_result_rejects_invalid_payload_before_network_and_keeps_lease(
    monkeypatch, bad_args
):
    seed()
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload)) or {"accepted": True},
    )
    result = server.call_tool(
        "workbuddy_submit_result",
        {"task_id": "task-validation", **bad_args},
    )
    assert result["isError"] is True
    assert calls == []
    assert "task-validation" in server.LEASES


def test_t176_long_task_id_uses_bounded_stable_default_result_key(monkeypatch):
    task_id = "t" * 128
    seed(task_id)
    calls = []
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload: calls.append((path, payload.copy())) or {"accepted": True},
    )
    result = server.call_tool(
        "workbuddy_submit_result",
        {"task_id": task_id, "state": "completed", "output": "done"},
    )
    assert result["isError"] is False
    result_key = calls[0][1]["result_key"]
    assert 8 <= len(result_key) <= 128
    assert result_key == "wb-result-" + hashlib.sha256(task_id.encode()).hexdigest()
    assert task_id not in server.LEASES



# T177 — claim filters are rejected locally before handshake or claim.
@pytest.mark.parametrize("agent_ids", [[], ["bad id"], ["../escape"], ["x" * 65], ["echo", "echo"], "echo", [None]])
def test_t177_claim_rejects_invalid_agent_filters_before_network(monkeypatch, agent_ids):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    result = server.call_tool("workbuddy_claim_task", {"agent_ids": agent_ids})
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("lease_seconds", [True, False, 14, 901, 1.5, "60", 0])
def test_t177_claim_rejects_invalid_lease_seconds_before_network(monkeypatch, lease_seconds):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    result = server.call_tool("workbuddy_claim_task", {"lease_seconds": lease_seconds})
    assert result["isError"] is True
    assert calls == []


def test_t177_claim_accepts_valid_filters_and_sends_normalized_payload(monkeypatch):
    calls = []
    task = {"id": "task-claim-valid", "agentId": "echo", "status": {"state": "submitted"}, "history": []}
    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {"claimed": False}
    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_claim_task", {"agent_ids": ["echo", "codex"], "lease_seconds": 60})
    assert result["isError"] is False
    assert calls == [("handshake", calls[0][1]), ("claim", {"agent_ids": ["echo", "codex"], "lease_seconds": 60})]


# T178 — claim receipts must use strict, bounded lease fields and task identity.
@pytest.mark.parametrize("expiry", [True, False, "9999999999", float("nan"), float("inf"), float("-inf"), 1, 10 ** 400])
def test_t178_claim_rejects_invalid_expiry_receipts_without_storing_lease(monkeypatch, expiry):
    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "claimed": True,
            "task": {"id": "task-claim-invalid", "history": []},
            "lease": {"lease_token": "opaque-token", "lease_version": 2, "lease_expires_at": expiry},
        }
    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert server.LEASES == {}


@pytest.mark.parametrize("task_id", [None, 7, "", "   ", "bad\\nkey", "x" * 129])
def test_t178_claim_rejects_invalid_task_identity_without_storing_lease(monkeypatch, task_id):
    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "claimed": True,
            "task": {"id": task_id, "history": []},
            "lease": {"lease_token": "opaque-token", "lease_version": 2, "lease_expires_at": time.time() + 120},
        }
    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert server.LEASES == {}


@pytest.mark.parametrize("version", [True, False, 0, -1, "2", 2.0, 2 ** 40])
def test_t178_claim_rejects_invalid_lease_versions_without_storing_lease(monkeypatch, version):
    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "claimed": True,
            "task": {"id": "task-claim-version", "history": []},
            "lease": {"lease_token": "opaque-token", "lease_version": version, "lease_expires_at": time.time() + 120},
        }
    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert server.LEASES == {}


# T179 — corrupt local lease entries are pruned safely and cannot crash claim.
@pytest.mark.parametrize("record", [None, [], "bad", {}, {"lease_token": "x", "lease_version": True, "lease_expires_at": time.time() + 60}, {"lease_token": "x", "lease_version": 1, "lease_expires_at": float("nan")}])
def test_t179_prune_removes_malformed_local_lease_records(record):
    with server.LEASES_LOCK:
        server.LEASES["corrupt-entry"] = record
    server._prune_expired_leases()
    assert "corrupt-entry" not in server.LEASES


def test_t179_lease_lookup_fails_closed_after_corrupt_entry_is_pruned():
    with server.LEASES_LOCK:
        server.LEASES["corrupt-entry"] = {"lease_token": None, "lease_version": 1, "lease_expires_at": time.time() + 60}
    with pytest.raises(RuntimeError, match="没有该任务的有效租约"):
        server._lease("corrupt-entry")
    assert "corrupt-entry" not in server.LEASES


def test_t179_prune_preserves_valid_unexpired_lease():
    seed("valid-local-lease")
    server._prune_expired_leases()
    assert "valid-local-lease" in server.LEASES


# T180 — heartbeat and cancellation share the strict task identity boundary.
@pytest.mark.parametrize("task_id", ["   ", "bad\\nkey", "x" * 129, 7, True])
def test_t180_heartbeat_rejects_invalid_current_task_id_before_network(monkeypatch, task_id):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    result = server.call_tool("workbuddy_heartbeat", {"status": "busy", "current_task_id": task_id})
    assert result["isError"] is True
    assert calls == []


@pytest.mark.parametrize("task_id", ["   ", "bad\\nkey", "x" * 129, 7, True])
def test_t180_cancellation_rejects_invalid_task_id_before_network(monkeypatch, task_id):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    result = server.call_tool("workbuddy_check_task_cancellation", {"task_id": task_id})
    assert result["isError"] is True
    assert calls == []


def test_t180_heartbeat_trims_valid_task_id_before_network(monkeypatch):
    calls = []
    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {"accepted": True, "connector_id": payload["connector_id"], "receipt_id": payload["heartbeat_id"], "status": "busy", "current_task_id": payload["current_task_id"], "cancel_requested": False}
    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_heartbeat", {"status": "busy", "current_task_id": " task-valid "})
    assert result["isError"] is False
    assert calls[-1][1]["current_task_id"] == "task-valid"


def test_t180_tool_schemas_publish_claim_and_task_id_bounds():
    claim = next(item for item in server.TOOLS if item["name"] == "workbuddy_claim_task")
    heartbeat = next(item for item in server.TOOLS if item["name"] == "workbuddy_heartbeat")
    assert claim["inputSchema"]["properties"]["agent_ids"]["maxItems"] == 32
    assert claim["inputSchema"]["properties"]["agent_ids"]["uniqueItems"] is True
    assert claim["inputSchema"]["properties"]["lease_seconds"]["minimum"] == 15
    assert heartbeat["inputSchema"]["properties"]["current_task_id"]["maxLength"] == 128
