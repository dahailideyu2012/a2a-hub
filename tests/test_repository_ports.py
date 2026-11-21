"""Structural conformance checks for provider-neutral repository ports."""

from __future__ import annotations

from inspect import signature
from typing import get_type_hints

from a2a_hub.ports import ClosableRepository, PullTaskRepository, TaskRepository
from a2a_hub.store import MemoryTaskStore, SqliteTaskStore, build_store


def _parameter_shape(method):
    return tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in signature(method).parameters.values()
    )


def _assert_signature_matches(protocol, implementation, method_name):
    expected = _parameter_shape(getattr(protocol, method_name))
    actual = _parameter_shape(getattr(implementation, method_name))
    assert actual == expected, (
        f"{implementation.__name__}.{method_name} signature {actual!r} "
        f"does not match {protocol.__name__}.{method_name} {expected!r}"
    )


def test_memory_store_implements_core_task_port():
    store = MemoryTaskStore()
    assert isinstance(store, TaskRepository)
    assert not isinstance(store, PullTaskRepository)
    assert not isinstance(store, ClosableRepository)
    for method_name in ("save", "get", "list", "delete"):
        _assert_signature_matches(TaskRepository, MemoryTaskStore, method_name)


def test_sqlite_store_implements_task_pull_and_lifecycle_ports(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "ports.db"))
    try:
        assert isinstance(store, TaskRepository)
        assert isinstance(store, PullTaskRepository)
        assert isinstance(store, ClosableRepository)
        for method_name in ("save", "get", "list", "delete"):
            _assert_signature_matches(TaskRepository, SqliteTaskStore, method_name)
        for method_name in (
            "enqueue_pull",
            "claim_next",
            "renew_pull_lease",
            "start_pull_task",
            "progress_pull_task",
            "finish_pull_task",
            "cancel_pull_task",
        ):
            _assert_signature_matches(PullTaskRepository, SqliteTaskStore, method_name)
    finally:
        store.close()


def test_build_store_exposes_the_repository_port_type():
    assert get_type_hints(build_store)["return"] is TaskRepository
