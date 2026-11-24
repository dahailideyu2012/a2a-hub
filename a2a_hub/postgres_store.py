"""PostgreSQL implementation of the synchronous task repository ports."""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .models import Artifact, Message, Task, TaskState
from .ports import RepositoryConflictError, RepositoryErrorCode, RepositoryNotFoundError
from .store import _INGRESS_TASK_IDEMPOTENCY_TTL, _ingress_idempotency_values, deserialize_task, serialize_task


_CLAIM_NEXT_SQL = """WITH candidate AS (
    SELECT id FROM tasks
    WHERE execution_mode='pull' AND state IN ('submitted','working')
      AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=%s)
      AND (%s::text[] IS NULL OR agent_id=ANY(%s::text[]))
    ORDER BY created ASC,id ASC
    LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE tasks t SET connector_id=%s,lease_token=%s,
    lease_version=t.lease_version+1,lease_expires_at=%s,lease_state='claimed'
FROM candidate c WHERE t.id=c.id
RETURNING t.payload,t.lease_version"""


class PostgresTaskStore:
    """Multi-process task store backed by PostgreSQL row locks and transactions."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8,
                 timeout: float = 5.0, migrate: bool = True) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        if min_size < 1 or max_size < min_size or timeout <= 0:
            raise ValueError("Invalid PostgreSQL pool configuration")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL backend requires psycopg and psycopg-pool; "
                "install the postgres optional dependency"
            ) from exc
        self._pool = ConnectionPool(
            conninfo=dsn, min_size=min_size, max_size=max_size,
            timeout=timeout,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": max(1, int(timeout)),
                # Apply the statement timeout to every pooled connection, not
                # only the one borrowed during startup validation.
                "options": f"-c statement_timeout={max(1, int(timeout * 1000))}",
            },
            open=False,
        )
        try:
            self._pool.open(wait=True, timeout=timeout)
            with self._pool.connection() as conn:
                if migrate:
                    from psycopg.pq import TransactionStatus

                    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "postgres" / "001_initial.sql"
                    # The migration owns its explicit BEGIN/COMMIT boundaries.
                    # Run it in autocommit mode so psycopg does not implicitly
                    # open an outer transaction before the script's BEGIN.
                    conn.autocommit = True
                    try:
                        conn.execute(sql_path.read_text(encoding="utf-8"), prepare=False)
                    except Exception:
                        if conn.info.transaction_status != TransactionStatus.IDLE:
                            conn.execute("ROLLBACK", prepare=False)
                        raise
                    finally:
                        conn.autocommit = False
                version = conn.execute(
                    "SELECT max(version) AS version FROM a2a_schema_migrations"
                ).fetchone()
                if not version or version["version"] is None or int(version["version"]) != 1:
                    raise RuntimeError("PostgreSQL schema version 1 is required; unsupported schema version")
                found = conn.execute(
                    """SELECT table_name,column_name FROM information_schema.columns
                       WHERE table_schema=current_schema()
                         AND table_name IN ('tasks','task_idempotency')"""
                ).fetchall()
                columns = {(row["table_name"], row["column_name"]) for row in found}
                required = {
                    ("tasks", name) for name in (
                        "id", "agent_id", "state", "created", "updated", "payload",
                        "execution_mode", "connector_id", "lease_token", "lease_version",
                        "lease_expires_at", "lease_state", "last_progress_seq",
                        "last_progress_hash", "result_key", "result_hash",
                    )
                } | {
                    ("task_idempotency", name) for name in (
                        "operation_id", "request_hash", "task_id", "created_at", "expires_at",
                    )
                }
                missing = sorted(required - columns)
                if missing:
                    raise RuntimeError(
                        "PostgreSQL schema is incomplete; missing columns: "
                        + ", ".join(f"{table}.{column}" for table, column in missing)
                    )
        except Exception:
            self._pool.close()
            raise RuntimeError("PostgreSQL backend initialization failed; verify connectivity and schema configuration") from None

    def _conn(self):
        return self._pool.connection()

    def save(self, task: Task) -> None:
        now = time.time()
        values = _ingress_idempotency_values(task)
        with self._conn() as conn:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO tasks(id,agent_id,state,created,updated,payload)
                       VALUES(%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(id) DO UPDATE SET agent_id=EXCLUDED.agent_id,
                       state=EXCLUDED.state,updated=EXCLUDED.updated,payload=EXCLUDED.payload""",
                    (task.id, task.agentId or "", task.status.state.value,
                     task.createdAt, task.updatedAt, serialize_task(task)),
                )
                if values:
                    op, digest = values
                    conn.execute("DELETE FROM task_idempotency WHERE expires_at < %s", (now,))
                    # INSERT ... ON CONFLICT serializes concurrent first writers.
                    # A SELECT FOR UPDATE alone cannot lock a missing key/gap in
                    # PostgreSQL, so two first writers could race into a raw
                    # unique-violation instead of the repository conflict contract.
                    conn.execute(
                        """INSERT INTO task_idempotency(operation_id,request_hash,task_id,created_at,expires_at)
                           VALUES(%s,%s,%s,%s,%s) ON CONFLICT(operation_id) DO NOTHING""",
                        (op, digest, task.id, now, now + _INGRESS_TASK_IDEMPOTENCY_TTL),
                    )
                    row = conn.execute(
                        "SELECT request_hash,task_id FROM task_idempotency WHERE operation_id=%s FOR UPDATE",
                        (op,),
                    ).fetchone()
                    if not row or row["request_hash"] != digest or row["task_id"] != task.id:
                        raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)

    def find_idempotent_task(self, operation_id: str, request_hash: str) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT i.request_hash,t.payload FROM task_idempotency i
                   LEFT JOIN tasks t ON t.id=i.task_id
                   WHERE i.operation_id=%s AND i.expires_at >= %s""",
                (operation_id, time.time()),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
        if row["payload"] is None:
            raise RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
        return deserialize_task(row["payload"])

    def get(self, task_id: str) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id=%s", (task_id,)).fetchone()
        return deserialize_task(row["payload"]) if row else None

    def list(self, limit: int = 100) -> list[Task]:
        limit = max(1, min(int(limit), 1000))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM tasks ORDER BY updated DESC,id ASC LIMIT %s", (limit,)
            ).fetchall()
        return [deserialize_task(row["payload"]) for row in rows]

    def delete(self, task_id: str) -> None:
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM task_idempotency WHERE task_id=%s", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id=%s", (task_id,))

    def enqueue_pull(self, task: Task) -> None:
        now = time.time()
        values = _ingress_idempotency_values(task)
        with self._conn() as conn:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO tasks(id,agent_id,state,created,updated,payload,execution_mode)
                       VALUES(%s,%s,%s,%s,%s,%s,'pull')""",
                    (task.id, task.agentId or "", task.status.state.value,
                     task.createdAt, task.updatedAt, serialize_task(task)),
                )
                if values:
                    op, digest = values
                    conn.execute("DELETE FROM task_idempotency WHERE expires_at < %s", (now,))
                    conn.execute(
                        """INSERT INTO task_idempotency(operation_id,request_hash,task_id,created_at,expires_at)
                           VALUES(%s,%s,%s,%s,%s) ON CONFLICT(operation_id) DO NOTHING""",
                        (op, digest, task.id, now, now + _INGRESS_TASK_IDEMPOTENCY_TTL),
                    )
                    row = conn.execute(
                        "SELECT request_hash,task_id FROM task_idempotency WHERE operation_id=%s FOR UPDATE", (op,)
                    ).fetchone()
                    if not row or row["request_hash"] != digest or row["task_id"] != task.id:
                        raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)

    @staticmethod
    def _valid_lease(row: dict[str, Any], connector: str, token: str,
                     version: int, now: float) -> bool:
        return bool(row and row["connector_id"] == connector
                    and row["lease_token"] == token
                    and int(row["lease_version"]) == int(version)
                    and row["lease_expires_at"] is not None
                    and float(row["lease_expires_at"]) > now
                    and row["execution_mode"] == "pull")

    def claim_next(self, connector_id: str, lease_seconds: int = 120,
                   agent_ids: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
        if not connector_id or not connector_id.strip():
            raise ValueError("connector_id must not be empty")
        if agent_ids is not None and not agent_ids:
            return None
        now = time.time()
        expires = now + max(15, min(int(lease_seconds), 900))
        token = secrets.token_urlsafe(32)
        with self._conn() as conn:
            with conn.transaction():
                row = conn.execute(
                    _CLAIM_NEXT_SQL,
                    (now, agent_ids, agent_ids, connector_id, token, expires),
                ).fetchone()
                if not row:
                    return None
                return {"task": deserialize_task(row["payload"]), "connector_id": connector_id,
                        "lease_token": token, "lease_version": int(row["lease_version"]),
                        "lease_expires_at": expires}

    def _leased(self, conn, task_id: str, connector: str, token: str, version: int, now: float):
        row = conn.execute(
            """SELECT payload,connector_id,lease_token,lease_version,lease_expires_at,
                      execution_mode,last_progress_seq,last_progress_hash,result_key,result_hash,lease_state
               FROM tasks WHERE id=%s FOR UPDATE""", (task_id,)
        ).fetchone()
        if not self._valid_lease(row, connector, token, version, now):
            raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return deserialize_task(row["payload"]), row

    def renew_pull_lease(self, task_id: str, connector_id: str, lease_token: str,
                         lease_version: int, lease_seconds: int = 120) -> float:
        now = time.time()
        expires = now + max(15, min(int(lease_seconds), 900))
        with self._conn() as conn:
            with conn.transaction():
                task, _ = self._leased(conn, task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                cur = conn.execute(
                    """UPDATE tasks SET lease_expires_at=%s WHERE id=%s AND connector_id=%s
                       AND lease_token=%s AND lease_version=%s AND lease_expires_at>%s""",
                    (expires, task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return expires

    def start_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                        lease_version: int) -> Task:
        now = time.time()
        with self._conn() as conn:
            with conn.transaction():
                task, row = self._leased(conn, task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                if task.status.state == TaskState.WORKING and row["lease_state"] == "running":
                    return task
                task.touch(TaskState.WORKING, Message.agent("WorkBuddy 已开始执行"))
                cur = conn.execute(
                    """UPDATE tasks SET state=%s,updated=%s,payload=%s,lease_state='running'
                       WHERE id=%s AND connector_id=%s AND lease_token=%s
                       AND lease_version=%s AND lease_expires_at>%s""",
                    (task.status.state.value, task.updatedAt, serialize_task(task),
                     task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return task

    def progress_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                           lease_version: int, sequence: int, message: str) -> tuple[Task, bool]:
        now = time.time()
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._conn() as conn:
            with conn.transaction():
                task, row = self._leased(conn, task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                previous = int(row["last_progress_seq"])
                if sequence == previous and row["last_progress_hash"] == digest:
                    return task, True
                if sequence <= previous:
                    raise RepositoryConflictError(RepositoryErrorCode.PROGRESS_SEQUENCE_CONFLICT)
                task.touch(TaskState.WORKING, Message.agent(message))
                task.history.append(Message.agent(message))
                cur = conn.execute(
                    """UPDATE tasks SET state=%s,updated=%s,payload=%s,last_progress_seq=%s,
                       last_progress_hash=%s WHERE id=%s AND connector_id=%s AND lease_token=%s
                       AND lease_version=%s AND lease_expires_at>%s""",
                    (task.status.state.value, task.updatedAt, serialize_task(task), sequence,
                     digest, task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return task, False

    def finish_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                         lease_version: int, result_key: str, state: str,
                         output: str) -> tuple[Task, bool]:
        now = time.time()
        digest = hashlib.sha256((state + "\0" + output).encode("utf-8")).hexdigest()
        final_state = TaskState.COMPLETED if state == "completed" else TaskState.FAILED
        with self._conn() as conn:
            with conn.transaction():
                old = conn.execute(
                    """SELECT payload,connector_id,lease_token,lease_version,execution_mode,
                              result_key,result_hash FROM tasks WHERE id=%s FOR UPDATE""",
                    (task_id,),
                ).fetchone()
                if old and old["execution_mode"] == "pull":
                    old_task = deserialize_task(old["payload"])
                    if old_task.status.is_terminal:
                        same_lease = (old["connector_id"] == connector_id
                                      and old["lease_token"] == lease_token
                                      and int(old["lease_version"]) == int(lease_version))
                        same_result = old["result_key"] == result_key and old["result_hash"] == digest
                        if same_lease and same_result:
                            return old_task, True
                        raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task, _ = self._leased(conn, task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task.history.append(Message.agent(output))
                if final_state == TaskState.COMPLETED and output:
                    task.add_artifact(Artifact.text(output, name="workbuddy-result"))
                task.touch(final_state, Message.agent(output or ("任务失败" if state == "failed" else "任务完成")))
                cur = conn.execute(
                    """UPDATE tasks SET state=%s,updated=%s,payload=%s,lease_state='finished',
                       result_key=%s,result_hash=%s WHERE id=%s AND connector_id=%s
                       AND lease_token=%s AND lease_version=%s AND lease_expires_at>%s""",
                    (task.status.state.value, task.updatedAt, serialize_task(task), result_key,
                     digest, task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return task, False

    def is_pull_task_cancelled(self, task_id: str, connector_id: str) -> bool:
        """Return cancellation only for a pull task assigned to this connector."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state,execution_mode,connector_id FROM tasks WHERE id=%s",
                (task_id,),
            ).fetchone()
        return bool(row and row["state"] == TaskState.CANCELED.value
                    and row["execution_mode"] == "pull"
                    and row["connector_id"] == connector_id)

    def cancel_pull_task(self, task_id: str) -> Task:
        now = time.time()
        with self._conn() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT payload,execution_mode FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
                ).fetchone()
                if not row:
                    raise RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
                if row["execution_mode"] != "pull":
                    raise RepositoryConflictError(RepositoryErrorCode.NOT_PULL_TASK)
                task = deserialize_task(row["payload"])
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                msg = "任务已由 ChatGPT 取消"
                task.history.append(Message.agent(msg))
                task.touch(TaskState.CANCELED, Message.agent(msg))
                conn.execute(
                    """UPDATE tasks SET state=%s,updated=%s,payload=%s,
                       lease_state='canceled',lease_expires_at=%s WHERE id=%s""",
                    (task.status.state.value, task.updatedAt, serialize_task(task), now, task_id),
                )
        return task

    def close(self) -> None:
        self._pool.close()


__all__ = ["PostgresTaskStore"]
