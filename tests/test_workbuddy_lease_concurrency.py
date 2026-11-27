from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import workbuddy_mcp_server as server


def _lease(task_id: str) -> None:
    with server.LEASES_LOCK:
        server.LEASES[task_id] = {
            "lease_token": "private-lease",
            "lease_version": 1,
            "lease_expires_at": time.time() + 120,
            "progress_sequence": 0,
            "progress_message": None,
        }


def test_concurrent_claims_never_exceed_single_active_lease(monkeypatch):
    calls = []
    calls_lock = threading.Lock()

    def fake_post(path, payload):
        with calls_lock:
            calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "claim":
            time.sleep(0.03)
            task_id = "claimed-task"
            return {
                "claimed": True,
                "task": {
                    "id": task_id,
                    "agentId": "codex",
                    "status": {"state": "submitted"},
                    "history": [],
                },
                "lease": {
                    "lease_token": "private-lease",
                    "lease_version": 1,
                    "lease_expires_at": time.time() + 120,
                },
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(server, "_post", fake_post)
    barrier = threading.Barrier(2)

    def claim_once():
        barrier.wait()
        return server.call_tool("workbuddy_claim_task", {})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim_once(), range(2)))

    assert sum(result["isError"] is False for result in results) == 1
    assert sum(path == "claim" for path in calls) == 1
    assert list(server.LEASES) == ["claimed-task"]


def test_progress_requires_contiguous_sequence_and_exact_duplicate_replay(monkeypatch):
    _lease("progress-task")
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        return {"accepted": True}

    monkeypatch.setattr(server, "_post", fake_post)

    first = server.call_tool("workbuddy_progress_task", {
        "task_id": "progress-task", "sequence": 1, "message": "started",
    })
    replay = server.call_tool("workbuddy_progress_task", {
        "task_id": "progress-task", "sequence": 1, "message": "started",
    })
    changed_duplicate = server.call_tool("workbuddy_progress_task", {
        "task_id": "progress-task", "sequence": 1, "message": "rewritten",
    })
    skipped = server.call_tool("workbuddy_progress_task", {
        "task_id": "progress-task", "sequence": 3, "message": "skipped sequence",
    })
    second = server.call_tool("workbuddy_progress_task", {
        "task_id": "progress-task", "sequence": 2, "message": "continued",
    })

    assert first["isError"] is False
    assert replay["isError"] is False
    assert changed_duplicate["isError"] is True
    assert skipped["isError"] is True
    assert second["isError"] is False
    assert [payload["sequence"] for _path, payload in calls] == [1, 1, 2]
    assert [payload["message"] for _path, payload in calls] == [
        "started", "started", "continued",
    ]
    assert server.LEASES["progress-task"]["progress_sequence"] == 2
    assert server.LEASES["progress-task"]["progress_message"] == "continued"


def test_concurrent_terminal_result_is_posted_only_once(monkeypatch):
    _lease("terminal-race")
    calls = []
    calls_lock = threading.Lock()
    entered_post = threading.Event()
    release_post = threading.Event()

    def fake_post(path, payload):
        with calls_lock:
            calls.append((path, payload.copy()))
        entered_post.set()
        assert release_post.wait(timeout=2)
        return {"accepted": True}

    monkeypatch.setattr(server, "_post", fake_post)

    def submit():
        return server.call_tool("workbuddy_submit_result", {
            "task_id": "terminal-race", "state": "completed", "output": "done",
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(submit)
        assert entered_post.wait(timeout=2)
        second_future = pool.submit(submit)
        release_post.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert sum(result["isError"] is False for result in (first, second)) == 1
    assert sum(result["isError"] is True for result in (first, second)) == 1
    assert len(calls) == 1
    assert "terminal-race" not in server.LEASES
