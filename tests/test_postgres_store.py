"""Opt-in PostgreSQL integration tests.

Set A2A_TEST_POSTGRES_DSN to a disposable PostgreSQL database to run these.
The normal test suite skips them without opening any network connection.
"""
from __future__ import annotations

import os
import time

import pytest

from a2a_hub.models import Task, TaskState
from a2a_hub.postgres_store import PostgresTaskStore
from a2a_hub.ports import RepositoryConflictError


DSN = os.environ.get("A2A_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="A2A_TEST_POSTGRES_DSN is not configured")


def _claim_in_independent_process(dsn: str, connector_id: str) -> str | None:
    """Create a fresh pool in a child process and claim one queued task."""
    store = PostgresTaskStore(dsn)
    try:
        claim = store.claim_next(connector_id, 60, ["echo-a"])
        return claim["task"].id if claim else None
    finally:
        store.close()


def test_postgres_pull_lifecycle_and_idempotent_replay():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    task_id = task.id
    try:
        store.enqueue_pull(task)
        claim = store.claim_next("integration-worker", 60, ["echo-a"])
        assert claim is not None
        assert claim["task"].id == task_id
        lease = claim
        started = store.start_pull_task(
            task_id, "integration-worker", lease["lease_token"], lease["lease_version"]
        )
        assert started.status.state == TaskState.WORKING

        progress, replay = store.progress_pull_task(
            task_id, "integration-worker", lease["lease_token"],
            lease["lease_version"], 1, "integration progress",
        )
        assert replay is False
        replayed_progress, replay = store.progress_pull_task(
            task_id, "integration-worker", lease["lease_token"],
            lease["lease_version"], 1, "integration progress",
        )
        assert replay is True
        assert replayed_progress.id == progress.id

        _result, replay = store.finish_pull_task(
            task_id, "integration-worker", lease["lease_token"],
            lease["lease_version"], task_id + ":result", "completed", "integration result",
        )
        assert replay is False
        replayed_result, replay = store.finish_pull_task(
            task_id, "integration-worker", lease["lease_token"],
            lease["lease_version"], task_id + ":result", "completed", "integration result",
        )
        assert replay is True
        assert replayed_result.status.state == TaskState.COMPLETED
        assert store.get(task_id).status.state == TaskState.COMPLETED
    finally:
        store.delete(task_id)
        store.close()


def test_postgres_independent_pools_claim_distinct_tasks_concurrently():
    first = PostgresTaskStore(DSN)
    second = PostgresTaskStore(DSN)
    tasks = [Task(agentId="echo-a"), Task(agentId="echo-a")]
    try:
        for task in tasks:
            first.enqueue_pull(task)
        from concurrent.futures import ProcessPoolExecutor
        # Separate OS processes and pools exercise PostgreSQL row locking rather
        # than relying on a shared Python lock or connection.
        with ProcessPoolExecutor(max_workers=2) as executor:
            claimed_ids = list(executor.map(
                _claim_in_independent_process,
                [DSN, DSN],
                ["worker-a", "worker-b"],
            ))
        claimed_ids = [item for item in claimed_ids if item]
        assert len(claimed_ids) == 2
        assert len(set(claimed_ids)) == 2
    finally:
        for task in tasks:
            first.delete(task.id)
        first.close()
        second.close()


def test_postgres_reclaimed_lease_fences_previous_worker():
    first = PostgresTaskStore(DSN)
    second = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    try:
        first.enqueue_pull(task)
        old = first.claim_next("old-worker", 60, ["echo-a"])
        assert old is not None

        # Expire the lease deterministically; do not sleep for the 15s minimum.
        with first._pool.connection() as conn:
            conn.execute(
                "UPDATE tasks SET lease_expires_at=%s WHERE id=%s",
                (time.time() - 1, task.id),
            )

        current = second.claim_next("new-worker", 60, ["echo-a"])
        assert current is not None
        assert current["task"].id == task.id
        assert current["lease_version"] == old["lease_version"] + 1
        assert current["lease_token"] != old["lease_token"]

        with pytest.raises(RepositoryConflictError):
            first.renew_pull_lease(
                task.id, "old-worker", old["lease_token"], old["lease_version"], 60
            )
        with pytest.raises(RepositoryConflictError):
            first.start_pull_task(
                task.id, "old-worker", old["lease_token"], old["lease_version"]
            )
        with pytest.raises(RepositoryConflictError):
            first.progress_pull_task(
                task.id, "old-worker", old["lease_token"], old["lease_version"],
                1, "stale progress",
            )
        with pytest.raises(RepositoryConflictError):
            first.finish_pull_task(
                task.id, "old-worker", old["lease_token"], old["lease_version"],
                "stale-result", "completed", "stale output",
            )

        completed, replay = second.finish_pull_task(
            task.id, "new-worker", current["lease_token"], current["lease_version"],
            task.id + ":result", "completed", "fresh output",
        )
        assert replay is False
        assert completed.status.state == TaskState.COMPLETED
    finally:
        first.delete(task.id)
        first.close()
        second.close()


def test_postgres_pull_enqueue_binds_idempotency_atomically():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a", metadata={
        "chatgpt_ingress_operation_id": "pg-operation-atomic",
        "chatgpt_ingress_request_hash": "pg-request-hash",
    })
    duplicate = Task(agentId="echo-a", metadata={
        "chatgpt_ingress_operation_id": "pg-operation-atomic",
        "chatgpt_ingress_request_hash": "pg-request-hash",
    })
    try:
        store.enqueue_pull(task)
        recovered = store.find_idempotent_task("pg-operation-atomic", "pg-request-hash")
        assert recovered is not None and recovered.id == task.id
        with pytest.raises(RepositoryConflictError):
            store.find_idempotent_task("pg-operation-atomic", "wrong-hash")
        with pytest.raises(RepositoryConflictError):
            store.enqueue_pull(duplicate)
        assert store.get(duplicate.id) is None
    finally:
        store.delete(task.id)
        store.delete(duplicate.id)
        store.close()
