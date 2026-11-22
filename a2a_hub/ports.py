"""Provider-neutral repository ports used by Hub services and routes.

These protocols describe behavior only. Concrete adapters (currently SQLite and
in-memory task stores) remain responsible for transactions and durability.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from .models import Task


class RepositoryErrorCode(str, Enum):
    """Stable machine-readable outcomes shared by repository adapters."""

    TASK_NOT_FOUND = "task_not_found"
    LEASE_CONFLICT = "lease_conflict"
    TASK_TERMINAL = "task_terminal"
    PROGRESS_SEQUENCE_CONFLICT = "progress_sequence_conflict"
    NOT_PULL_TASK = "not_pull_task"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class RepositoryError:
    """Mixin carrying a stable repository outcome code."""

    code: RepositoryErrorCode

    def __init__(self, code: RepositoryErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class RepositoryConflictError(RepositoryError, ValueError):
    """A task state, lease, or replay conflict (also a ValueError for compatibility)."""


class RepositoryNotFoundError(RepositoryError, KeyError):
    """A required task does not exist (also a KeyError for compatibility)."""


@runtime_checkable
class TaskRepository(Protocol):
    """Core task persistence contract shared by Hub callers.

    ``get`` returns ``None`` when a task is absent. ``find_idempotent_task``
    returns ``None`` when no unexpired mapping exists and raises
    ``RepositoryConflictError(IDEMPOTENCY_CONFLICT)`` for a hash mismatch.
    Adapter/driver failures propagate unchanged; callers translate them at the
    transport boundary.
    """

    def save(self, task: Task) -> None: ...
    def get(self, task_id: str) -> Optional[Task]: ...
    def list(self, limit: int = 100) -> list[Task]: ...
    def delete(self, task_id: str) -> None: ...
    def find_idempotent_task(self, operation_id: str, request_hash: str) -> Optional[Task]: ...


@runtime_checkable
class PullTaskRepository(TaskRepository, Protocol):
    """Atomic Pull-queue and lease-fencing contract.

    Implementations must preserve the transaction and idempotency invariants in
    docs/storage-contract-v1.md. HTTP and RPC layers must depend on this port,
    never on a concrete adapter class.

    Expected domain errors are stable across adapters: ``RepositoryNotFoundError``
    carries ``TASK_NOT_FOUND``; ``RepositoryConflictError`` carries one of
    ``LEASE_CONFLICT``, ``TASK_TERMINAL``, ``PROGRESS_SEQUENCE_CONFLICT``,
    ``NOT_PULL_TASK``, or ``IDEMPOTENCY_CONFLICT``. Both retain KeyError/ValueError compatibility for existing
    callers. Unexpected adapter/driver exceptions propagate unchanged.
    """

    def enqueue_pull(self, task: Task) -> None: ...
    def claim_next(
        self,
        connector_id: str,
        lease_seconds: int = 120,
        agent_ids: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]: ...
    def renew_pull_lease(
        self,
        task_id: str,
        connector_id: str,
        lease_token: str,
        lease_version: int,
        lease_seconds: int = 120,
    ) -> float: ...
    def start_pull_task(
        self, task_id: str, connector_id: str, lease_token: str, lease_version: int
    ) -> Task: ...
    def progress_pull_task(
        self,
        task_id: str,
        connector_id: str,
        lease_token: str,
        lease_version: int,
        sequence: int,
        message: str,
    ) -> tuple[Task, bool]: ...
    def finish_pull_task(
        self,
        task_id: str,
        connector_id: str,
        lease_token: str,
        lease_version: int,
        result_key: str,
        state: str,
        output: str,
    ) -> tuple[Task, bool]: ...
    def cancel_pull_task(self, task_id: str) -> Task: ...


@runtime_checkable
class GithubCallbackOutboxRepository(Protocol):
    """Durable at-least-once delivery queue for GitHub Issue result callbacks."""

    def claim_github_callback(self, lease_seconds: int = 60) -> Optional[dict[str, Any]]: ...
    def complete_github_callback(self, outbox_id: int) -> None: ...
    def retry_github_callback(self, outbox_id: int, error: str, delay_seconds: int) -> None: ...


class ClosableRepository(Protocol):
    """Optional lifecycle capability for adapters that own local resources."""

    def close(self) -> None: ...


__all__ = [
    "RepositoryErrorCode",
    "RepositoryError",
    "RepositoryConflictError",
    "RepositoryNotFoundError",
    "TaskRepository",
    "PullTaskRepository",
    "GithubCallbackOutboxRepository",
    "ClosableRepository",
]
