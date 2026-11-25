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
                    last_heartbeat_seq INTEGER,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                )
                """
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS workbuddy_connection_events (
                    connector_id TEXT PRIMARY KEY,
                    last_error_code TEXT,
                    last_error_at REAL,
                    reconnect_attempt INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(workbuddy_presence)")}
            if "last_heartbeat_seq" not in columns:
                db.execute("ALTER TABLE workbuddy_presence ADD COLUMN last_heartbeat_seq INTEGER")

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
                    status, current_task_id, last_heartbeat_id, last_heartbeat_seq,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 'idle', NULL, NULL, NULL, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    protocol_version=excluded.protocol_version,
                    client_version=excluded.client_version,
                    capabilities_json=excluded.capabilities_json,
                    status='idle',
                    current_task_id=NULL,
                    last_heartbeat_id=NULL,
                    last_heartbeat_seq=NULL,
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
            db.execute("""
                INSERT INTO workbuddy_connection_events
                    (connector_id, last_error_code, last_error_at, reconnect_attempt)
                VALUES (?, NULL, NULL, 0)
                ON CONFLICT(connector_id) DO UPDATE SET
                    last_error_code=NULL, last_error_at=NULL, reconnect_attempt=0
            """, (connector_id,))

    def record_heartbeat(
        self,
        connector_id: str,
        heartbeat_id: str,
        status: str,
        current_task_id: str | None,
        heartbeat_seq: int | None = None,
    ) -> bool:
        if heartbeat_seq is not None and (
            isinstance(heartbeat_seq, bool) or not isinstance(heartbeat_seq, int) or heartbeat_seq < 1
        ):
            raise ValueError("heartbeat sequence must be a positive integer")
        now = time.time()
        with closing(self._connect()) as db, db:
            if heartbeat_seq is None:
                cursor = db.execute(
                    """
                    UPDATE workbuddy_presence
                    SET status = ?, current_task_id = ?, last_heartbeat_id = ?, last_seen_at = ?
                    WHERE connector_id = ?
                    """,
                    (status, current_task_id, heartbeat_id, now, connector_id),
                )
            else:
                cursor = db.execute(
                    """
                    UPDATE workbuddy_presence
                    SET status = ?, current_task_id = ?, last_heartbeat_id = ?,
                        last_heartbeat_seq = ?, last_seen_at = ?
                    WHERE connector_id = ?
                      AND (last_heartbeat_seq IS NULL OR last_heartbeat_seq < ?)
                    """,
                    (status, current_task_id, heartbeat_id, heartbeat_seq, now,
                     connector_id, heartbeat_seq),
                )
            accepted = cursor.rowcount == 1
            if accepted:
                db.execute("""
                    INSERT INTO workbuddy_connection_events
                        (connector_id, last_error_code, last_error_at, reconnect_attempt)
                    VALUES (?, NULL, NULL, 0)
                    ON CONFLICT(connector_id) DO UPDATE SET
                        last_error_code=NULL, last_error_at=NULL, reconnect_attempt=0
                """, (connector_id,))
            return accepted

    def record_error(self, connector_id: str, error_code: str) -> None:
        """Persist only a bounded error category, never raw exception text."""
        allowed = {
            "network_unreachable", "timeout", "authentication_failed",
            "protocol_mismatch", "hub_unavailable", "unknown",
        }
        if not isinstance(error_code, str) or error_code not in allowed:
            raise ValueError("unsupported connector error code")
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO workbuddy_connection_events
                    (connector_id, last_error_code, last_error_at, reconnect_attempt)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(connector_id) DO UPDATE SET
                    last_error_code=excluded.last_error_code,
                    last_error_at=excluded.last_error_at
            """, (connector_id, error_code, now))

    def record_reconnect_attempt(self, connector_id: str) -> int:
        """Atomically increment the durable retry counter."""
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO workbuddy_connection_events
                    (connector_id, last_error_code, last_error_at, reconnect_attempt)
                VALUES (?, NULL, NULL, 1)
                ON CONFLICT(connector_id) DO UPDATE SET
                    reconnect_attempt=reconnect_attempt+1
            """, (connector_id,))
            row = db.execute(
                "SELECT reconnect_attempt FROM workbuddy_connection_events WHERE connector_id=?",
                (connector_id,),
            ).fetchone()
            return int(row["reconnect_attempt"])

    def get_runtime_events(self, connector_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT last_error_code, last_error_at, reconnect_attempt "
                "FROM workbuddy_connection_events WHERE connector_id=?",
                (connector_id,),
            ).fetchone()
        if row is None:
            return {"last_error_code": None, "last_error_at": None, "reconnect_attempt": 0}
        return dict(row)

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
            "last_heartbeat_seq": row["last_heartbeat_seq"],
            "protocol_version": row["protocol_version"],
            "client_version": row["client_version"],
            "capabilities": json.loads(row["capabilities_json"]),
            "first_seen_at": self._iso(float(row["first_seen_at"])),
            "last_seen_at": self._iso(last_seen),
            "seconds_since_seen": max(0.0, current - last_seen),
            "ttl_seconds": self.ttl_seconds,
        }
