"""Structural conformance checks for provider-neutral repository ports."""

from __future__ import annotations

from inspect import signature
from typing import get_type_hints

import pytest

from a2a_hub.ports import (
    ClosableRepository,
    PullTaskRepository,
    RepositoryConflictError,
    RepositoryErrorCode,
    RepositoryNotFoundError,
    TaskRepository,
)
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
    for method_name in ("save", "get", "list", "delete", "find_idempotent_task"):
        _assert_signature_matches(TaskRepository, MemoryTaskStore, method_name)


def test_sqlite_store_implements_task_pull_and_lifecycle_ports(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "ports.db"))
    try:
        assert isinstance(store, TaskRepository)
        assert isinstance(store, PullTaskRepository)
        assert isinstance(store, ClosableRepository)
        for method_name in ("save", "get", "list", "delete", "find_idempotent_task"):
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


def test_repository_errors_expose_stable_codes_and_legacy_base_types():
    assert issubclass(RepositoryConflictError, ValueError)
    assert issubclass(RepositoryNotFoundError, KeyError)
    conflict = RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
    missing = RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
    assert conflict.code is RepositoryErrorCode.LEASE_CONFLICT
    assert str(conflict) == "lease_conflict"
    assert missing.code is RepositoryErrorCode.TASK_NOT_FOUND


def test_memory_store_recovers_idempotent_task_mapping():
    from a2a_hub.models import Task

    store = MemoryTaskStore()
    task = Task(metadata={
        "chatgpt_ingress_operation_id": "operation-123",
        "chatgpt_ingress_request_hash": "request-hash",
    })
    store.save(task)
    assert store.find_idempotent_task("operation-123", "request-hash").id == task.id
    with pytest.raises(RepositoryConflictError):
        store.find_idempotent_task("operation-123", "different-hash")


def test_sqlite_store_recovers_idempotent_task_mapping_after_reopen(tmp_path):
    from a2a_hub.models import Task

    path = tmp_path / "idempotency.db"
    task = Task(metadata={
        "chatgpt_ingress_operation_id": "operation-456",
        "chatgpt_ingress_request_hash": "request-hash",
    })
    store = SqliteTaskStore(str(path))
    store.save(task)
    store.close()
    reopened = SqliteTaskStore(str(path))
    try:
        recovered = reopened.find_idempotent_task("operation-456", "request-hash")
        assert recovered is not None and recovered.id == task.id
        with pytest.raises(RepositoryConflictError):
            reopened.find_idempotent_task("operation-456", "different-hash")
        duplicate = Task(metadata={
            "chatgpt_ingress_operation_id": "operation-456",
            "chatgpt_ingress_request_hash": "request-hash",
        })
        with pytest.raises(RepositoryConflictError):
            reopened.save(duplicate)
        assert reopened.get(duplicate.id) is None
    finally:
        reopened.close()


def test_sqlite_pull_enqueue_records_idempotency_atomically(tmp_path):
    from a2a_hub.models import Task

    store = SqliteTaskStore(str(tmp_path / "pull-idempotency.db"))
    task = Task(metadata={
        "chatgpt_ingress_operation_id": "operation-pull",
        "chatgpt_ingress_request_hash": "request-hash",
    })
    try:
        store.enqueue_pull(task)
        recovered = store.find_idempotent_task("operation-pull", "request-hash")
        assert recovered is not None and recovered.id == task.id
    finally:
        store.close()
