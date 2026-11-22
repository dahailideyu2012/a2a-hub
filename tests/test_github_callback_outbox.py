from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from a2a_hub.github_callback_outbox import GithubCallbackOutbox, run_outbox_worker
from a2a_hub.github_result_callback import GithubResultDeliveryError

CALLBACK = {"repository": "owner/repo", "issue_number": 42, "source_comment_id": 9}


def test_outbox_persists_and_reclaims_expired_lease(tmp_path):
    path = tmp_path / "outbox.db"
    box = GithubCallbackOutbox(path)
    assert box.enqueue("task-1", CALLBACK, "completed", "done")
    assert not box.enqueue("task-1", CALLBACK, "completed", "done")
    item = box.claim_due()
    assert item["attempts"] == 1 and item["lease_token"]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE github_callback_outbox SET lease_until=?", (time.time() - 1,))
    again = GithubCallbackOutbox(path).claim_due()
    assert again["task_id"] == "task-1" and again["attempts"] == 2
    assert again["lease_token"] != item["lease_token"]


def test_stale_worker_cannot_acknowledge_or_fail_reclaimed_lease(tmp_path):
    box = GithubCallbackOutbox(tmp_path / "fenced.db")
    box.enqueue("task-fence", CALLBACK, "completed", "done")
    stale = box.claim_due()
    with sqlite3.connect(box.path) as conn:
        conn.execute(
            "UPDATE github_callback_outbox SET lease_until=? WHERE task_id=?",
            (time.time() - 1, "task-fence"),
        )
    current = box.claim_due()
    assert current["lease_token"] != stale["lease_token"]
    assert box.mark_delivered("task-fence", stale["lease_token"]) is False
    assert box.mark_failed(
        "task-fence", stale["attempts"], stale["lease_token"], RuntimeError("stale")
    ) is False
    assert box.status("task-fence")["status"] == "processing"
    assert box.mark_delivered("task-fence", current["lease_token"]) is True
    assert box.status("task-fence")["status"] == "delivered"


def test_concurrent_outbox_claim_has_one_winner(tmp_path):
    path = str(tmp_path / "concurrent.db")
    seed = GithubCallbackOutbox(path)
    seed.enqueue("task-race", CALLBACK, "completed", "done")

    def claim(_worker: int):
        return GithubCallbackOutbox(path).claim_due()

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["task_id"] == "task-race"


def test_existing_t6_schema_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE github_callback_outbox (
                task_id TEXT PRIMARY KEY, callback_json TEXT NOT NULL, state TEXT NOT NULL,
                output TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
                lease_until REAL, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )"""
        )
        now = time.time()
        conn.execute(
            """INSERT INTO github_callback_outbox
               (task_id,callback_json,state,output,status,attempts,next_attempt_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("legacy-task", '{"repository":"owner/repo"}', "completed", "kept", "pending", 0, now, now, now),
        )
    box = GithubCallbackOutbox(path)
    claimed = box.claim_due()
    assert claimed["task_id"] == "legacy-task"
    assert claimed["output"] == "kept" and claimed["lease_token"]


def test_backoff_dead_letter_and_manual_retry(tmp_path):
    box = GithubCallbackOutbox(tmp_path / "outbox.db", max_attempts=2, base_backoff=1)
    box.enqueue("task-2", CALLBACK, "failed", "oops")
    one = box.claim_due()
    assert box.mark_failed(
        "task-2", one["attempts"], one["lease_token"], GithubResultDeliveryError("safe")
    )
    assert box.status("task-2")["status"] == "pending"
    with sqlite3.connect(box.path) as conn:
        conn.execute("UPDATE github_callback_outbox SET next_attempt_at=?", (time.time() - 1,))
    two = box.claim_due()
    assert box.mark_failed(
        "task-2", two["attempts"], two["lease_token"], RuntimeError("private payload")
    )
    assert box.status("task-2")["status"] == "dead"
    assert box.retry("task-2") and box.status("task-2")["attempts"] == 0
    assert "private payload" not in str(box.status("task-2"))


@pytest.mark.asyncio
async def test_worker_delivers_and_marks_success(tmp_path):
    box = GithubCallbackOutbox(tmp_path / "outbox.db")
    box.enqueue("task-3", CALLBACK, "completed", "done")
    calls = []

    def fake(*args):
        calls.append(args)
        return {"delivered": True}

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_outbox_worker(
            box, stop, api_token="synthetic", allowed_repos="owner/repo",
            poll_seconds=0.05, deliver=fake,
        )
    )
    for _ in range(50):
        if box.status("task-3")["status"] == "delivered":
            break
        await asyncio.sleep(0.02)
    stop.set()
    await worker
    assert len(calls) == 1 and box.status("task-3")["status"] == "delivered"


@pytest.mark.asyncio
async def test_worker_retries_transient_failure(tmp_path):
    box = GithubCallbackOutbox(tmp_path / "outbox.db", base_backoff=1)
    box.enqueue("task-4", CALLBACK, "completed", "done")
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise GithubResultDeliveryError("transient")
        return {"delivered": True}

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_outbox_worker(
            box, stop, api_token="synthetic", allowed_repos="owner/repo",
            poll_seconds=0.02, deliver=flaky,
        )
    )
    await asyncio.sleep(1.2)
    stop.set()
    await worker
    assert len(calls) >= 2 and box.status("task-4")["status"] == "delivered"
