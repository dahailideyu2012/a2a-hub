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
    store = PostgresTaskStore(dsn, migrate=False)
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


def test_postgres_independent_processes_claim_distinct_tasks_concurrently():
    setup = PostgresTaskStore(DSN)
    tasks = [Task(agentId="echo-a"), Task(agentId="echo-a")]
    try:
        for task in tasks:
            setup.enqueue_pull(task)
    finally:
        # Close all parent-side pool threads before the process pool forks/spawns.
        setup.close()

    try:
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
        cleanup = PostgresTaskStore(DSN)
        try:
            for task in tasks:
                cleanup.delete(task.id)
        finally:
            cleanup.close()

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


def test_postgres_core_task_repository_contract():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    try:
        assert store.get(task.id) is None
        store.save(task)
        loaded = store.get(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.agentId == "echo-a"
        assert any(item.id == task.id for item in store.list(limit=100))

        task.touch(TaskState.WORKING)
        store.save(task)
        assert store.get(task.id).status.state == TaskState.WORKING

        store.delete(task.id)
        assert store.get(task.id) is None
    finally:
        store.delete(task.id)
        store.close()

def test_postgres_startup_fails_closed_when_database_is_unreachable():
    from psycopg.conninfo import make_conninfo

    # Override the test DSN to loopback port 1 so this test never contacts
    # the configured database or any external host.
    unreachable = make_conninfo(
        DSN, host="127.0.0.1", port=1, connect_timeout=1
    )
    with pytest.raises(RuntimeError, match="PostgreSQL backend initialization failed"):
        PostgresTaskStore(unreachable, timeout=0.5)


def test_postgres_closed_pool_rejects_new_repository_operations():
    from psycopg_pool import PoolClosed

    store = PostgresTaskStore(DSN)
    store.close()
    with pytest.raises(PoolClosed):
        store.get("closed-pool-probe")

def test_postgres_cancel_fences_the_active_worker():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    try:
        store.enqueue_pull(task)
        claim = store.claim_next("cancel-worker", 60, ["echo-a"])
        assert claim is not None
        canceled = store.cancel_pull_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

        with pytest.raises(RepositoryConflictError):
            store.renew_pull_lease(
                task.id, "cancel-worker", claim["lease_token"], claim["lease_version"], 60
            )
        with pytest.raises(RepositoryConflictError):
            store.start_pull_task(
                task.id, "cancel-worker", claim["lease_token"], claim["lease_version"]
            )
        with pytest.raises(RepositoryConflictError):
            store.progress_pull_task(
                task.id, "cancel-worker", claim["lease_token"], claim["lease_version"],
                1, "must be rejected after cancellation",
            )
        with pytest.raises(RepositoryConflictError):
            store.finish_pull_task(
                task.id, "cancel-worker", claim["lease_token"], claim["lease_version"],
                "cancelled-result", "completed", "must not overwrite cancellation",
            )
    finally:
        store.delete(task.id)
        store.close()

def test_postgres_start_is_idempotent_for_the_same_active_lease():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    try:
        store.enqueue_pull(task)
        claim = store.claim_next("pg-start-idempotent", 120, ["echo-a"])
        assert claim is not None
        first = store.start_pull_task(
            task.id, "pg-start-idempotent", claim["lease_token"], claim["lease_version"]
        )
        first_history_size = len(first.history)
        first_updated_at = first.updatedAt

        replay = store.start_pull_task(
            task.id, "pg-start-idempotent", claim["lease_token"], claim["lease_version"]
        )
        assert len(replay.history) == first_history_size
        assert replay.updatedAt == first_updated_at
        assert replay.status.state == TaskState.WORKING
    finally:
        store.delete(task.id)
        store.close()



def test_postgres_is_pull_task_cancelled_is_connector_scoped():
    store = PostgresTaskStore(DSN)
    task = Task(agentId="echo-a")
    try:
        store.enqueue_pull(task)
        claim = store.claim_next("cancel-scope-worker", 60, ["echo-a"])
        assert claim is not None
        store.cancel_pull_task(task.id)
        assert store.is_pull_task_cancelled(task.id, "cancel-scope-worker") is True
        assert store.is_pull_task_cancelled(task.id, "another-worker") is False
    finally:
        store.delete(task.id)
        store.close()
