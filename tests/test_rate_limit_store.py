from __future__ import annotations

import multiprocessing
import time

import pytest

from a2a_hub.rate_limit_store import SQLiteRateLimitStore


def _allow_once(path: str, key: str, limit: int, queue) -> None:
    store = SQLiteRateLimitStore(path, busy_timeout_ms=30_000)
    try:
        queue.put(store.allow(key, limit, 60))
    finally:
        queue.close()


def test_sliding_window_limit_and_exact_expiry_boundary(tmp_path):
    store = SQLiteRateLimitStore(tmp_path / "limits.db")
    assert store.allow("bucket", 2, 60, now=1000)
    assert store.allow("bucket", 2, 60, now=1001)
    assert not store.allow("bucket", 2, 60, now=1059.9)
    # The first hit is exactly 60 seconds old and is outside the half-open window.
    assert store.allow("bucket", 2, 60, now=1060)
    assert not store.allow("bucket", 2, 60, now=1060)
    

def test_buckets_are_isolated_and_clock_rollback_cannot_reset_quota(tmp_path):
    store = SQLiteRateLimitStore(tmp_path / "limits.db")
    assert store.allow("a", 1, 60, now=1000)
    assert not store.allow("a", 1, 60, now=900)
    assert store.allow("b", 1, 60, now=900)


def test_purge_expired_removes_hits_and_empty_bucket_clock(tmp_path):
    store = SQLiteRateLimitStore(tmp_path / "limits.db")
    assert store.allow("old", 3, 60, now=1000)
    assert store.allow("fresh", 3, 60, now=2000)
    assert store.purge_expired(now=1060, window_seconds=60) == 1
    assert store.allow("old", 1, 60, now=1060)


def test_empty_key_is_rejected(tmp_path):
    store = SQLiteRateLimitStore(tmp_path / "limits.db")
    with pytest.raises(ValueError, match="key"):
        store.allow("", 1, 60)


def test_storage_failure_raises_instead_of_allowing(tmp_path):
    # A directory cannot be opened as a SQLite database file.
    store = SQLiteRateLimitStore(tmp_path)
    with pytest.raises(Exception):
        store.allow("bucket", 1, 60)


def test_multiprocess_atomic_budget_is_never_exceeded(tmp_path):
    path = str(tmp_path / "shared.db")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_allow_once, args=(path, "shared", 4, queue)) for _ in range(12)]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=45) for _ in workers]
    for worker in workers:
        worker.join(timeout=45)
        assert worker.exitcode == 0
    assert sum(results) == 4
