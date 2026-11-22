from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from a2a_hub.github_outbox_worker import run_github_outbox_worker
from a2a_hub.models import Message, Task
from a2a_hub.store import SqliteTaskStore


def _finished_task(store: SqliteTaskStore, output: str = "synthetic result") -> str:
    task = Task(agentId="echo-a", metadata={
        "github_issue_callback": {
            "repository": "owner/repo",
            "issue_number": 7,
            "source_comment_id": 88,
        }
    })
    task.history.append(Message.user("synthetic prompt"))
    store.enqueue_pull(task)
    lease = store.claim_next("test-worker", 120)
    assert lease is not None
    store.finish_pull_task(
        task.id, "test-worker", lease["lease_token"], lease["lease_version"],
        "synthetic-result-key", "completed", output,
    )
    return task.id


def test_terminal_result_and_outbox_row_commit_atomically(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "atomic.db"))
    task = Task(agentId="echo-a", metadata={
        "github_issue_callback": {
            "repository": "owner/repo", "issue_number": 7, "source_comment_id": 88
        }
    })
    store.enqueue_pull(task)
    lease = store.claim_next("test-worker", 120)
    assert lease is not None
    store._conn.execute(
        "CREATE TRIGGER reject_outbox BEFORE INSERT ON github_result_outbox "
        "BEGIN SELECT RAISE(ABORT, 'synthetic outbox failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.finish_pull_task(
            task.id, "test-worker", lease["lease_token"], lease["lease_version"],
            "atomic-result-key", "completed", "synthetic result",
        )
    assert store.get(task.id).status.state.value == "submitted"
    assert store._conn.execute("SELECT COUNT(*) FROM github_result_outbox").fetchone()[0] == 0
    store.close()


def test_outbox_claim_complete_and_same_task_unique(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "queue.db"))
    task_id = _finished_task(store)
    item = store.claim_github_callback()
    assert item and item["task_id"] == task_id
    assert item["attempt_count"] == 1
    assert store.claim_github_callback() is None
    store.complete_github_callback(item["id"])
    assert store.claim_github_callback() is None
    assert store._conn.execute(
        "SELECT state FROM github_result_outbox WHERE task_id=?", (task_id,)
    ).fetchone()[0] == "delivered"
    store.close()


def test_expired_outbox_lock_can_be_reclaimed_after_worker_crash(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "reclaim.db"))
    task_id = _finished_task(store)
    first = store.claim_github_callback(lease_seconds=15)
    assert first and first["task_id"] == task_id
    store._conn.execute(
        "UPDATE github_result_outbox SET locked_until=? WHERE id=?",
        (time.time() - 1, first["id"]),
    )
    second = store.claim_github_callback(lease_seconds=15)
    assert second and second["id"] == first["id"]
    assert second["attempt_count"] == 2
    store.close()


def test_retry_schedules_future_attempt_and_preserves_row(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "retry.db"))
    task_id = _finished_task(store)
    item = store.claim_github_callback()
    assert item
    store.retry_github_callback(item["id"], "synthetic failure", 60)
    row = store._conn.execute(
        "SELECT state,next_attempt_at,locked_until,last_error FROM github_result_outbox WHERE task_id=?",
        (task_id,),
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] > time.time() + 30
    assert row[2] is None
    assert row[3] == "synthetic failure"
    assert store.claim_github_callback() is None
    store.close()


def test_background_worker_marks_success_without_workbuddy_replay(tmp_path, monkeypatch):
    store = SqliteTaskStore(str(tmp_path / "worker-success.db"))
    _finished_task(store)
    stop = asyncio.Event()
    calls = []

    def deliver(callback, task_id, state, output, token, allowed):
        calls.append((callback, task_id, state, output, token, allowed))
        stop.set()
        return {"delivered": True, "replayed": False}

    monkeypatch.setattr("a2a_hub.github_outbox_worker.deliver_github_issue_result", deliver)
    asyncio.run(run_github_outbox_worker(store, "synthetic-token-0123456789", "owner/repo", stop))
    assert len(calls) == 1
    assert store._conn.execute(
        "SELECT state FROM github_result_outbox"
    ).fetchone()[0] == "delivered"
    store.close()


def test_background_worker_schedules_retry_on_delivery_failure(tmp_path, monkeypatch):
    store = SqliteTaskStore(str(tmp_path / "worker-failure.db"))
    _finished_task(store)
    stop = asyncio.Event()

    def fail(callback, task_id, state, output, token, allowed):
        stop.set()
        raise __import__("a2a_hub.github_result_callback", fromlist=["GithubResultDeliveryError"]).GithubResultDeliveryError("synthetic")

    monkeypatch.setattr("a2a_hub.github_outbox_worker.deliver_github_issue_result", fail)
    asyncio.run(run_github_outbox_worker(store, "synthetic-token-0123456789", "owner/repo", stop))
    row = store._conn.execute(
        "SELECT state,next_attempt_at,attempt_count,locked_until FROM github_result_outbox"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] > time.time()
    assert row[2] == 1
    assert row[3] is None
    store.close()
