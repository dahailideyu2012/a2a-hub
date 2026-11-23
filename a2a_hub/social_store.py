"""Durable SQLite storage for IM conversations.

The IM domain model remains in-memory for fast reads. This adapter stores each
conversation as one atomic JSON snapshot so messages, receipts, read markers and
conversation metadata survive process restarts together.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class SqliteSocialStore:
    """Thread-safe, atomic snapshot store for conversations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=10.0, check_same_thread=False
        )
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS social_conversations (
                    id TEXT PRIMARY KEY,
                    position INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_conversations_order "
                "ON social_conversations(position)"
            )
            self._conn.commit()

    def load(self) -> list[str]:
        """Return serialized conversations in their original list order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM social_conversations ORDER BY position ASC, id ASC"
            ).fetchall()
            return [str(row[0]) for row in rows]

    def save(self, conversation_id: str, position: int, updated_at: str, payload: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO social_conversations(id, position, updated_at, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        position=excluded.position,
                        updated_at=excluded.updated_at,
                        payload=excluded.payload
                    """,
                    (conversation_id, position, updated_at, payload),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM social_conversations WHERE id = ?",
                    (conversation_id,),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def reorder(self, conversation_ids: list[str]) -> None:
        """Normalize positions after deletion/eviction to keep reload order stable."""
        with self._lock:
            try:
                self._conn.executemany(
                    "UPDATE social_conversations SET position = ? WHERE id = ?",
                    [(position, conversation_id) for position, conversation_id in enumerate(conversation_ids)],
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SqliteSocialStore"]
