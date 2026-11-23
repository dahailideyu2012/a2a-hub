"""Durable, single-connector WorkBuddy presence registry backed by SQLite."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkBuddyPresenceStore:
    """Persist connector liveness; online is derived from the heartbeat TTL."""

    def __init__(self, db_path: Path | str, ttl_seconds: int = 90) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS workbuddy_presence (
                    connector_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id TEXT,
                    last_heartbeat_id TEXT,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    @staticmethod
    def _iso(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def record_handshake(
        self,
        connector_id: str,
        protocol_version: str,
        client_version: str,
        capabilities: list[str],
    ) -> None:
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute(
                """
                INSERT INTO workbuddy_presence (
                    connector_id, protocol_version, client_version, capabilities_json,
                    status, current_task_id, last_heartbeat_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 'idle', NULL, NULL, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    protocol_version=excluded.protocol_version,
                    client_version=excluded.client_version,
                    capabilities_json=excluded.capabilities_json,
                    status='idle',
                    current_task_id=NULL,
                    last_heartbeat_id=NULL,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    connector_id,
                    protocol_version,
                    client_version,
                    json.dumps(sorted(set(capabilities))),
                    now,
                    now,
                ),
            )

    def record_heartbeat(
        self,
        connector_id: str,
        heartbeat_id: str,
        status: str,
        current_task_id: str | None,
    ) -> bool:
        now = time.time()
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                """
                UPDATE workbuddy_presence
                SET status = ?, current_task_id = ?, last_heartbeat_id = ?, last_seen_at = ?
                WHERE connector_id = ?
                """,
                (status, current_task_id, heartbeat_id, now, connector_id),
            )
            return cursor.rowcount == 1

    def get_presence(self, connector_id: str, now: float | None = None) -> dict[str, Any] | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM workbuddy_presence WHERE connector_id = ?",
                (connector_id,),
            ).fetchone()
        if row is None:
            return None
        current = time.time() if now is None else now
        last_seen = float(row["last_seen_at"])
        online = max(0.0, current - last_seen) <= self.ttl_seconds
        return {
            "connector_id": row["connector_id"],
            "online": online,
            "status": row["status"] if online else "offline",
            "last_status": row["status"],
            "current_task_id": row["current_task_id"] if online else None,
            "last_heartbeat_id": row["last_heartbeat_id"],
            "protocol_version": row["protocol_version"],
            "client_version": row["client_version"],
            "capabilities": json.loads(row["capabilities_json"]),
            "first_seen_at": self._iso(float(row["first_seen_at"])),
            "last_seen_at": self._iso(last_seen),
            "seconds_since_seen": max(0.0, current - last_seen),
            "ttl_seconds": self.ttl_seconds,
        }
