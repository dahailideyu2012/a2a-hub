"""Provider-neutral repository ports used by Hub services and routes.

These protocols describe behavior only. Concrete adapters (currently SQLite and
in-memory task stores) remain responsible for transactions and durability.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .models import Task


@runtime_checkable
class TaskRepository(Protocol):
    """Core task persistence contract shared by Hub callers.

    ``get`` returns ``None`` when a task is absent. Adapter/driver failures
    propagate unchanged; callers translate them at the transport boundary.
    """

    def save(self, task: Task) -> None: ...
    def get(self, task_id: str) -> Optional[Task]: ...
    def list(self, limit: int = 100) -> list[Task]: ...
    def delete(self, task_id: str) -> None: ...


@runtime_checkable
class PullTaskRepository(TaskRepository, Protocol):
    """Atomic Pull-queue and lease-fencing contract.

    Implementations must preserve the transaction and idempotency invariants in
    docs/storage-contract-v1.md. HTTP and RPC layers must depend on this port,
    never on a concrete adapter class.

    Expected domain errors are stable across adapters: ``KeyError`` denotes a
    missing task where the operation requires one; ``ValueError`` denotes an
    invalid transition, lease-fencing failure, or conflicting replay. Unexpected
    adapter/driver exceptions are not silently converted into domain errors.
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
class ClosableRepository(Protocol):
    """Optional lifecycle capability for adapters that own local resources."""

    def close(self) -> None: ...


__all__ = ["TaskRepository", "PullTaskRepository", "ClosableRepository"]
