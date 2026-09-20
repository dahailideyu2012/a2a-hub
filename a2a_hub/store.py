"""任务与上下文的存储层 —— memory / sqlite 两种后端。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import Task


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

    def save(self, task: Task) -> None:
        with self._lock:
            if task.id not in self._data:
                self._order.append(task.id)
            self._data[task.id] = task
            while len(self._order) > self._max:
                old = self._order.pop(0)
                self._data.pop(old, None)

    def get(self, task_id: str) -> Optional[Task]:
        return self._data.get(task_id)

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
                payload   TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated DESC)")
        self._conn.commit()

    def save(self, task: Task) -> None:
        payload = serialize_task(task)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(id, agent_id, state, created, updated, payload) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "agent_id=excluded.agent_id, state=excluded.state, "
                "updated=excluded.updated, payload=excluded.payload",
                (
                    task.id,
                    task.agentId or "",
                    task.status.state.value,
                    task.createdAt,
                    task.updatedAt,
                    payload,
                ),
            )
            self._conn.commit()

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

    def close(self) -> None:
        self._conn.close()


def build_store(kind: str, db_path: str) -> TaskStore:
    if kind.lower() == "sqlite":
        return SqliteTaskStore(db_path)
    return MemoryTaskStore()


__all__ = [
    "TaskStore",
    "MemoryTaskStore",
    "SqliteTaskStore",
    "build_store",
    "serialize_task",
    "deserialize_task",
    "task_to_wire",
]
