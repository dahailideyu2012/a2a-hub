from __future__ import annotations

import threading
import time

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
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


def _claim_response(task_id="task-claim", *, token="private-token", version=1, expires_at=None):
    return {
        "claimed": True,
        "task": {"id": task_id, "agentId": "test-agent", "status": {"state": "submitted"}, "history": []},
        "lease": {
            "lease_token": token,
            "lease_version": version,
            "lease_expires_at": expires_at if expires_at is not None else time.time() + 120,
        },
    }


def _seed_lease(task_id="task-race", token="old-token", version=3):
    with server.LEASES_LOCK:
        server.LEASES[task_id] = {
            "lease_token": token,
            "lease_version": version,
            "lease_expires_at": time.time() + 120,
        }


def test_concurrent_claims_are_serialized_and_only_one_remote_claim_is_sent(monkeypatch):
    claim_entered = threading.Event()
    release_claim = threading.Event()
    claim_calls = []

    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "claim":
            claim_calls.append(path)
            claim_entered.set()
            assert release_claim.wait(5)
            return _claim_response()
        raise AssertionError(path)

    monkeypatch.setattr(server, "_post", fake_post)
    first = threading.Thread(target=lambda: server.call_tool("workbuddy_claim_task", {}))
    second_result = {}
    second_started = threading.Event()

    def second_claim():
        second_started.set()
        second_result["value"] = server.call_tool("workbuddy_claim_task", {})

    second = threading.Thread(target=second_claim)
    first.start()
    assert claim_entered.wait(5)
    second.start()
    assert second_started.wait(5)
    release_claim.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert len(claim_calls) == 1
    assert second_result["value"]["isError"] is True
    assert "已有未完成任务" in second_result["value"]["content"][0]["text"]
    assert list(server.LEASES) == ["task-claim"]


@pytest.mark.parametrize(
    ("token", "version", "expires_at"),
    [
        ("", 1, time.time() + 60),
        ("private-token", True, time.time() + 60),
        ("private-token", 0, time.time() + 60),
        ("private-token", "1", time.time() + 60),
        ("private-token", 1, "nan"),
        ("private-token", 1, "inf"),
        ("private-token", 1, time.time() - 1),
    ],
)
def test_claim_rejects_malformed_lease_receipts(token, version, expires_at, monkeypatch):
    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return _claim_response(token=token, version=version, expires_at=expires_at)

    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert server.LEASES == {}


def test_claim_rejects_missing_claimed_boolean(monkeypatch):
    monkeypatch.setattr(server, "_post", lambda path, payload: (
        {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "handshake" else {"task": {}, "lease": {}}
    ))
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert server.LEASES == {}


def test_stale_renew_receipt_cannot_update_replacement_lease(monkeypatch):
    renew_entered = threading.Event()
    release_renew = threading.Event()

    def fake_post(path, payload):
        if path.endswith("/renew"):
            renew_entered.set()
            assert release_renew.wait(5)
            return {"lease_expires_at": time.time() + 900}
        raise AssertionError(path)

    _seed_lease()
    monkeypatch.setattr(server, "_post", fake_post)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "value", server.call_tool("workbuddy_renew_task", {"task_id": "task-race"})
    ))
    worker.start()
    assert renew_entered.wait(5)
    with server.LEASES_LOCK:
        server.LEASES["task-race"] = {
            "lease_token": "replacement-token",
            "lease_version": 4,
            "lease_expires_at": time.time() + 60,
        }
    release_renew.set()
    worker.join(5)

    assert not worker.is_alive()
    assert result["value"]["isError"] is True
    assert server.LEASES["task-race"]["lease_token"] == "replacement-token"
    assert server.LEASES["task-race"]["lease_version"] == 4


def test_stale_result_receipt_cannot_remove_replacement_lease(monkeypatch):
    result_entered = threading.Event()
    release_result = threading.Event()

    def fake_post(path, payload):
        if path.endswith("/result"):
            result_entered.set()
            assert release_result.wait(5)
            return {"accepted": True}
        raise AssertionError(path)

    _seed_lease()
    monkeypatch.setattr(server, "_post", fake_post)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "value", server.call_tool("workbuddy_submit_result", {
            "task_id": "task-race", "state": "completed", "output": "done",
        })
    ))
    worker.start()
    assert result_entered.wait(5)
    with server.LEASES_LOCK:
        server.LEASES["task-race"] = {
            "lease_token": "replacement-token",
            "lease_version": 4,
            "lease_expires_at": time.time() + 60,
        }
    release_result.set()
    worker.join(5)

    assert not worker.is_alive()
    assert result["value"]["isError"] is True
    assert server.LEASES["task-race"]["lease_token"] == "replacement-token"
    assert server.LEASES["task-race"]["lease_version"] == 4
