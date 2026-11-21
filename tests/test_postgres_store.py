"""Opt-in PostgreSQL integration tests.

Set A2A_TEST_POSTGRES_DSN to a disposable PostgreSQL database to run these.
The normal test suite skips them without opening any network connection.
"""
from __future__ import annotations

import os

import pytest

from a2a_hub.models import Task, TaskState
from a2a_hub.postgres_store import PostgresTaskStore


DSN = os.environ.get("A2A_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="A2A_TEST_POSTGRES_DSN is not configured")


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
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(
                lambda pair: pair[0].claim_next(pair[1], 60, ["echo-a"]),
                [(first, "worker-a"), (second, "worker-b")],
            ))
        claimed_ids = [item["task"].id for item in claims if item]
        assert len(claimed_ids) == 2
        assert len(set(claimed_ids)) == 2
    finally:
        for task in tasks:
            first.delete(task.id)
        first.close()
        second.close()
