"""Structural conformance checks for provider-neutral repository ports."""

from __future__ import annotations

from a2a_hub.ports import ClosableRepository, PullTaskRepository, TaskRepository
from a2a_hub.store import MemoryTaskStore, SqliteTaskStore


def test_memory_store_implements_core_task_port():
    store = MemoryTaskStore()
    assert isinstance(store, TaskRepository)
    assert not isinstance(store, PullTaskRepository)
    assert not isinstance(store, ClosableRepository)


def test_sqlite_store_implements_task_pull_and_lifecycle_ports(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "ports.db"))
    try:
        assert isinstance(store, TaskRepository)
        assert isinstance(store, PullTaskRepository)
        assert isinstance(store, ClosableRepository)
    finally:
        store.close()
