"""任务与上下文的存储层 —— memory / sqlite 两种后端。"""

from __future__ import annotations

import json
import sqlite3
import threading
import secrets
import hashlib
import time
from pathlib import Path
from typing import Optional

from .models import Artifact, Message, Task, TaskState
from .ports import (
    RepositoryConflictError,
    RepositoryErrorCode,
    RepositoryNotFoundError,
    TaskRepository,
)


_INGRESS_TASK_IDEMPOTENCY_TTL = 24 * 60 * 60


def _ingress_idempotency_values(task: Task) -> tuple[str, str] | None:
    operation_id = task.metadata.get("chatgpt_ingress_operation_id")
    request_hash = task.metadata.get("chatgpt_ingress_request_hash")
    if operation_id is None and request_hash is None:
        return None
    if not isinstance(operation_id, str) or not operation_id or not isinstance(request_hash, str) or not request_hash:
        raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
    return operation_id, request_hash


def serialize_task(task: Task) -> str:
    """持久化序列化。

    Task 的 ``agentId`` / ``createdAt`` / ``updatedAt`` 被标了 ``exclude=True``，
    因为它们不属于 A2A 线上载荷（那些属于 Hub 的内部扩展）。但持久化必须
    保留它们，否则重启后任务列表就丢了归属信息——所以在这里显式补回。
    """
    data = task.model_dump(mode="json")
    data["agentId"] = task.agentId
    data["createdAt"] = task.createdAt
    data["updatedAt"] = task.updatedAt
    return json.dumps(data, ensure_ascii=False)


def deserialize_task(raw: str) -> Task:
    return Task.model_validate(json.loads(raw))


def task_to_wire(task: Task, include_internal: bool = False) -> dict:
    """转成对外 JSON。

    ``include_internal=True`` 时额外带上 Hub 内部字段（REST 视图用），
    ``False`` 时是纯净的 A2A 线上格式。
    """
    data = task.model_dump(mode="json")
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("chatgpt_ingress_operation_id", None)
        metadata.pop("chatgpt_ingress_request_hash", None)
        metadata.pop("github_issue_callback", None)
    if include_internal:
        data["agentId"] = task.agentId
        data["createdAt"] = task.createdAt
        data["updatedAt"] = task.updatedAt
    return data


class TaskStore:
    """抽象接口。"""

    def save(self, task: Task) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, task_id: str) -> Optional[Task]:  # pragma: no cover
        raise NotImplementedError

    def list(self, limit: int = 100) -> list[Task]:  # pragma: no cover
        raise NotImplementedError

    def delete(self, task_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


class MemoryTaskStore(TaskStore):
    def __init__(self, max_items: int = 2000) -> None:
        self._data: dict[str, Task] = {}
        self._order: list[str] = []
        self._max = max_items
        self._lock = threading.Lock()
        self._idempotency: dict[str, tuple[str, str, float]] = {}

    def save(self, task: Task) -> None:
        with self._lock:
            values = _ingress_idempotency_values(task)
            if values is not None:
                operation_id, request_hash = values
                now = time.time()
                existing = self._idempotency.get(operation_id)
                if existing and existing[2] >= now:
                    if existing[0] != request_hash or existing[1] != task.id:
                        raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
                else:
                    self._idempotency[operation_id] = (request_hash, task.id, now + _INGRESS_TASK_IDEMPOTENCY_TTL)
            if task.id not in self._data:
                self._order.append(task.id)
            self._data[task.id] = task
            while len(self._order) > self._max:
                old = self._order.pop(0)
                self._data.pop(old, None)

    def get(self, task_id: str) -> Optional[Task]:
        return self._data.get(task_id)

    def find_idempotent_task(self, operation_id: str, request_hash: str) -> Optional[Task]:
        with self._lock:
            entry = self._idempotency.get(operation_id)
            if entry is None:
                return None
            if entry[2] < time.time():
                self._idempotency.pop(operation_id, None)
                return None
            if entry[0] != request_hash:
                raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
            task = self._data.get(entry[1])
            if task is None:
                raise RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
            return task

    def list(self, limit: int = 100) -> list[Task]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._data[i] for i in ids if i in self._data]

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._data.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)


class SqliteTaskStore(TaskStore):
    """轻量持久化。写入频繁但每次都是整条 Task 覆盖，够用且零运维。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id        TEXT PRIMARY KEY,
                agent_id  TEXT,
                state     TEXT,
                created   TEXT,
                updated   TEXT,
                payload   TEXT NOT NULL,
                execution_mode TEXT NOT NULL DEFAULT 'hub',
                connector_id TEXT,
                lease_token TEXT,
                lease_version INTEGER NOT NULL DEFAULT 0,
                lease_expires_at REAL,
                lease_state TEXT,
                last_progress_seq INTEGER NOT NULL DEFAULT 0,
                last_progress_hash TEXT,
                result_key TEXT,
                result_hash TEXT
            )
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        migrations = {
            "execution_mode": "TEXT NOT NULL DEFAULT 'hub'",
            "connector_id": "TEXT",
            "lease_token": "TEXT",
            "lease_version": "INTEGER NOT NULL DEFAULT 0",
            "lease_expires_at": "REAL",
            "lease_state": "TEXT",
            "last_progress_seq": "INTEGER NOT NULL DEFAULT 0",
            "last_progress_hash": "TEXT",
            "result_key": "TEXT",
            "result_hash": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated DESC)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pull_queue ON tasks(execution_mode, state, created)")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS task_idempotency (
                operation_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_task_idempotency_task ON task_idempotency(task_id)")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS github_result_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                callback_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                result_state TEXT NOT NULL,
                output TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_github_result_outbox_due "
            "ON github_result_outbox(state,next_attempt_at,locked_until)"
        )
        self._conn.commit()

    def _remember_idempotency(self, task: Task) -> None:
        values = _ingress_idempotency_values(task)
        if values is None:
            return
        operation_id, request_hash = values
        now = time.time()
        self._conn.execute("DELETE FROM task_idempotency WHERE expires_at < ?", (now,))
        existing = self._conn.execute(
            "SELECT request_hash, task_id FROM task_idempotency WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing:
            if existing[0] != request_hash or existing[1] != task.id:
                raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
            return
        self._conn.execute(
            "INSERT INTO task_idempotency(operation_id,request_hash,task_id,created_at,expires_at) "
            "VALUES(?,?,?,?,?)",
            (operation_id, request_hash, task.id, now, now + _INGRESS_TASK_IDEMPOTENCY_TTL),
        )

    def save(self, task: Task) -> None:
        payload = serialize_task(task)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO tasks(id, agent_id, state, created, updated, payload) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "agent_id=excluded.agent_id, state=excluded.state, "
                    "updated=excluded.updated, payload=excluded.payload",
                    (task.id, task.agentId or "", task.status.state.value,
                     task.createdAt, task.updatedAt, payload),
                )
                self._remember_idempotency(task)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def find_idempotent_task(self, operation_id: str, request_hash: str) -> Optional[Task]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT i.request_hash, t.payload FROM task_idempotency i "
                "LEFT JOIN tasks t ON t.id=i.task_id "
                "WHERE i.operation_id=? AND i.expires_at>=?",
                (operation_id, now),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise RepositoryConflictError(RepositoryErrorCode.IDEMPOTENCY_CONFLICT)
        if row[1] is None:
            raise RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
        return deserialize_task(row[1])

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return Task.model_validate(json.loads(row[0])) if row else None

    def list(self, limit: int = 100) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Task.model_validate(json.loads(r[0])) for r in rows]

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()

    def enqueue_pull(self, task: Task) -> None:
        """Persist a pull-mode task before it becomes visible to Connectors."""
        payload = serialize_task(task)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO tasks(id,agent_id,state,created,updated,payload,execution_mode) "
                    "VALUES(?,?,?,?,?,?, 'pull')",
                    (task.id, task.agentId or "", task.status.state.value,
                     task.createdAt, task.updatedAt, payload),
                )
                self._remember_idempotency(task)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _lease_matches(row: tuple, connector_id: str, lease_token: str,
                       lease_version: int, now: float) -> bool:
        return bool(row and row[0] == connector_id and row[1] == lease_token
                    and int(row[2]) == int(lease_version)
                    and row[3] is not None and float(row[3]) > now
                    and row[4] == "pull")

    def claim_next(self, connector_id: str, lease_seconds: int = 120,
                   agent_ids: Optional[list[str]] = None) -> Optional[dict]:
        """Atomically claim one pull task. Single-host SQLite only."""
        now = time.time()
        token = secrets.token_urlsafe(32)
        expires = now + max(15, min(int(lease_seconds), 900))
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                sql = ("SELECT id FROM tasks WHERE execution_mode='pull' "
                       "AND state IN ('submitted','working') "
                       "AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=?)")
                args: list = [now]
                if agent_ids is not None:
                    if not agent_ids:
                        conn.commit()
                        return None
                    sql += " AND agent_id IN (" + ",".join("?" for _ in agent_ids) + ")"
                    args.extend(agent_ids)
                sql += " ORDER BY created ASC LIMIT 1"
                found = conn.execute(sql, args).fetchone()
                if not found:
                    conn.commit()
                    return None
                task_id = str(found[0])
                cur = conn.execute(
                    "UPDATE tasks SET connector_id=?,lease_token=?,lease_version=lease_version+1,"
                    "lease_expires_at=?,lease_state='claimed' "
                    "WHERE id=? AND execution_mode='pull' AND state IN ('submitted','working') "
                    "AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=?)",
                    (connector_id, token, expires, task_id, now),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                row = conn.execute(
                    "SELECT payload,lease_version FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                conn.commit()
                return {
                    "task": deserialize_task(row[0]),
                    "connector_id": connector_id,
                    "lease_token": token,
                    "lease_version": int(row[1]),
                    "lease_expires_at": expires,
                }
            except Exception:
                conn.rollback()
                raise

    def _leased_task(self, task_id: str, connector_id: str, lease_token: str,
                     lease_version: int, now: float):
        row = self._conn.execute(
            "SELECT payload,connector_id,lease_token,lease_version,lease_expires_at,"
            "execution_mode,last_progress_seq,last_progress_hash,result_key,result_hash "
            "FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row or not self._lease_matches(
            (row[1], row[2], row[3], row[4], row[5]),
            connector_id, lease_token, lease_version, now
        ):
            raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
        return deserialize_task(row[0]), row

    def renew_pull_lease(self, task_id: str, connector_id: str, lease_token: str,
                         lease_version: int, lease_seconds: int = 120) -> float:
        now = time.time()
        expires = now + max(15, min(int(lease_seconds), 900))
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                task, _ = self._leased_task(task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                cur = conn.execute(
                    "UPDATE tasks SET lease_expires_at=? WHERE id=? AND connector_id=? "
                    "AND lease_token=? AND lease_version=? AND lease_expires_at>?",
                    (expires, task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
                conn.commit()
                return expires
            except Exception:
                conn.rollback()
                raise

    def start_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                        lease_version: int) -> Task:
        now = time.time()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                task, _ = self._leased_task(task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task.touch(TaskState.WORKING, Message.agent("WorkBuddy 已开始执行"))
                conn.execute(
                    "UPDATE tasks SET state=?,updated=?,payload=?,lease_state='running' "
                    "WHERE id=? AND connector_id=? AND lease_token=? AND lease_version=? AND lease_expires_at>?",
                    (task.status.state.value, task.updatedAt, serialize_task(task), task_id,
                     connector_id, lease_token, lease_version, now),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
                conn.commit()
                return task
            except Exception:
                conn.rollback()
                raise

    def progress_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                           lease_version: int, sequence: int, message: str) -> tuple[Task, bool]:
        now = time.time()
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                task, row = self._leased_task(task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                previous_seq, previous_hash = int(row[6]), row[7]
                if sequence == previous_seq and previous_hash == digest:
                    conn.commit()
                    return task, True
                if sequence <= previous_seq:
                    raise RepositoryConflictError(RepositoryErrorCode.PROGRESS_SEQUENCE_CONFLICT)
                task.touch(TaskState.WORKING, Message.agent(message))
                task.history.append(Message.agent(message))
                cur = conn.execute(
                    "UPDATE tasks SET state=?,updated=?,payload=?,last_progress_seq=?,last_progress_hash=? "
                    "WHERE id=? AND connector_id=? AND lease_token=? AND lease_version=? AND lease_expires_at>?",
                    (task.status.state.value, task.updatedAt, serialize_task(task), sequence, digest,
                     task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
                conn.commit()
                return task, False
            except Exception:
                conn.rollback()
                raise

    def finish_pull_task(self, task_id: str, connector_id: str, lease_token: str,
                         lease_version: int, result_key: str, state: str,
                         output: str) -> tuple[Task, bool]:
        now = time.time()
        digest = hashlib.sha256((state + "\0" + output).encode("utf-8")).hexdigest()
        final_state = TaskState.COMPLETED if state == "completed" else TaskState.FAILED
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # A terminal result replay is idempotent even after lease expiry,
                # but only for the exact original connector/token/version and payload.
                # A reclaimed lease has a different token/version and cannot replay.
                existing = conn.execute(
                    "SELECT payload,connector_id,lease_token,lease_version,execution_mode,"
                    "result_key,result_hash FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if existing and existing[4] == "pull":
                    existing_task = deserialize_task(existing[0])
                    if existing_task.status.is_terminal:
                        same_lease = (
                            existing[1] == connector_id
                            and existing[2] == lease_token
                            and int(existing[3]) == int(lease_version)
                        )
                        same_result = existing[5] == result_key and existing[6] == digest
                        if same_lease and same_result:
                            conn.commit()
                            return existing_task, True
                        raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task, row = self._leased_task(task_id, connector_id, lease_token, lease_version, now)
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task.history.append(Message.agent(output))
                if final_state == TaskState.COMPLETED and output:
                    task.add_artifact(Artifact.text(output, name="workbuddy-result"))
                task.touch(final_state, Message.agent(output or ("任务失败" if state == "failed" else "任务完成")))
                cur = conn.execute(
                    "UPDATE tasks SET state=?,updated=?,payload=?,lease_state='finished',"
                    "result_key=?,result_hash=? "
                    "WHERE id=? AND connector_id=? AND lease_token=? AND lease_version=? AND lease_expires_at>?",
                    (task.status.state.value, task.updatedAt, serialize_task(task),
                     result_key, digest, task_id, connector_id, lease_token, lease_version, now),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflictError(RepositoryErrorCode.LEASE_CONFLICT)
                callback = task.metadata.get("github_issue_callback")
                if isinstance(callback, dict):
                    callback_json = json.dumps(callback, ensure_ascii=False, sort_keys=True)
                    conn.execute(
                        "INSERT OR IGNORE INTO github_result_outbox "
                        "(task_id,callback_json,state,result_state,output,attempt_count,next_attempt_at,created_at,updated_at) "
                        "VALUES(?,?,'pending',?,?,0,?,?,?)",
                        (task_id, callback_json, state, output, now, now, now),
                    )
                conn.commit()
                return task, False
            except Exception:
                conn.rollback()
                raise

    def claim_github_callback(self, lease_seconds: int = 60) -> Optional[dict]:
        now = time.time()
        expires = now + max(15, min(int(lease_seconds), 300))
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id,task_id,callback_json,result_state,output,attempt_count "
                    "FROM github_result_outbox WHERE state='pending' AND next_attempt_at<=? "
                    "AND (locked_until IS NULL OR locked_until<=?) ORDER BY id LIMIT 1",
                    (now, now),
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                cur = conn.execute(
                    "UPDATE github_result_outbox SET locked_until=?,attempt_count=attempt_count+1,updated_at=? "
                    "WHERE id=? AND state='pending' AND (locked_until IS NULL OR locked_until<=?)",
                    (expires, now, row[0], now),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return {
                    "id": int(row[0]), "task_id": row[1],
                    "callback": json.loads(row[2]), "state": row[3],
                    "output": row[4], "attempt_count": int(row[5]) + 1,
                }
            except Exception:
                conn.rollback()
                raise

    def complete_github_callback(self, outbox_id: int) -> None:
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE github_result_outbox SET state='delivered',locked_until=NULL,last_error=NULL,updated_at=? "
                "WHERE id=? AND state='pending'",
                (now, outbox_id),
            )
            if cur.rowcount != 1:
                raise ValueError("github_callback_outbox_conflict")

    def retry_github_callback(self, outbox_id: int, error: str, delay_seconds: int) -> None:
        now = time.time()
        safe_error = (error or "delivery failed")[:200]
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE github_result_outbox SET next_attempt_at=?,locked_until=NULL,last_error=?,updated_at=? "
                "WHERE id=? AND state='pending'",
                (now + max(1, min(int(delay_seconds), 86400)), safe_error, now, outbox_id),
            )
            if cur.rowcount != 1:
                raise ValueError("github_callback_outbox_conflict")

    def cancel_pull_task(self, task_id: str) -> Task:
        """Cancel a pull task without invoking an agent adapter."""
        now = time.time()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload,execution_mode FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise RepositoryNotFoundError(RepositoryErrorCode.TASK_NOT_FOUND)
                if row[1] != "pull":
                    raise RepositoryConflictError(RepositoryErrorCode.NOT_PULL_TASK)
                task = deserialize_task(row[0])
                if task.status.is_terminal:
                    raise RepositoryConflictError(RepositoryErrorCode.TASK_TERMINAL)
                task.history.append(Message.agent("任务已由 ChatGPT 取消"))
                task.touch(TaskState.CANCELED, Message.agent("任务已由 ChatGPT 取消"))
                conn.execute(
                    "UPDATE tasks SET state=?,updated=?,payload=?,lease_state='canceled',"
                    "lease_expires_at=? WHERE id=? AND execution_mode='pull'",
                    (task.status.state.value, task.updatedAt, serialize_task(task), now, task_id),
                )
                conn.commit()
                return task
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()


def build_store(kind: str, db_path: str) -> TaskRepository:
    """Build only an explicitly selected backend; never silently downgrade.

    A typo such as postgres or sqllite must not create an in-memory store
    that loses accepted tasks on restart. Backend selection is a durability
    boundary, so unsupported values fail during startup.
    """
    normalized = kind.strip().lower()
    if normalized == "sqlite":
        return SqliteTaskStore(db_path)
    if normalized == "memory":
        return MemoryTaskStore()
    raise ValueError(
        f"Unsupported A2A_STORE backend: {kind!r}. "
        "Supported backends are: memory, sqlite."
    )


__all__ = [
    "TaskStore",
    "MemoryTaskStore",
    "SqliteTaskStore",
    "build_store",
    "serialize_task",
    "deserialize_task",
    "task_to_wire",
]
