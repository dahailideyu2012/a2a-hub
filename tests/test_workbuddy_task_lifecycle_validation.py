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


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf"), True, "9999999999", 1, 10 ** 10000])
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
