"""Durable local connector state, liveness classification, and retry policy."""
from __future__ import annotations

import math
import random
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

SUPPORTED_PROTOCOL_VERSIONS = ("1.0",)
CONNECTOR_ERROR_CODES = frozenset({
    "network_unreachable", "timeout", "authentication_failed",
    "protocol_mismatch", "hub_unavailable", "unknown",
})
_CONNECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEARTBEAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SCHEMA_VERSION = 2


def negotiate_protocol(requested: str) -> str:
    """Return the exact mutually supported protocol or fail closed."""
    if not isinstance(requested, str) or requested not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError("unsupported connector protocol version")
    return requested


def classify_connection_age(seconds_since_seen: float | int | None, *,
                            heartbeat_interval_seconds: int = 30,
                            presence_ttl_seconds: int = 90) -> dict[str, Any]:
    """Classify heartbeat age as online, degraded, or offline."""
    if (isinstance(heartbeat_interval_seconds, bool)
            or isinstance(presence_ttl_seconds, bool)
            or heartbeat_interval_seconds < 1
            or presence_ttl_seconds < heartbeat_interval_seconds):
        raise ValueError("invalid heartbeat/TTL configuration")
    if seconds_since_seen is None:
        age, state = None, "offline"
    else:
        age = float(seconds_since_seen)
        if not math.isfinite(age):
            raise ValueError("heartbeat age must be finite")
        age = max(0.0, age)
        online_threshold = min(heartbeat_interval_seconds * 2, presence_ttl_seconds)
        if age <= online_threshold:
            state = "online"
        elif age <= presence_ttl_seconds:
            state = "degraded"
        else:
            state = "offline"
    return {"state": state, "seconds_since_seen": age, "stale": state != "online"}


def reconnect_delay(attempt: int, *, base_seconds: float = 1.0,
                    max_seconds: float = 60.0, jitter_ratio: float = 0.2,
                    random_value: float | None = None) -> float:
    """Bounded exponential backoff with optional jitter."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    if (not math.isfinite(base_seconds) or not math.isfinite(max_seconds)
            or base_seconds <= 0 or max_seconds < base_seconds):
        raise ValueError("invalid backoff bounds")
    if not math.isfinite(jitter_ratio) or not 0 <= jitter_ratio <= 1:
        raise ValueError("jitter_ratio must be between 0 and 1")
    sample = random.random() if random_value is None else float(random_value)
    if not math.isfinite(sample):
        raise ValueError("random_value must be finite")
    if not 0 <= sample <= 1:
        raise ValueError("random_value must be between 0 and 1")
    ceiling = min(max_seconds, base_seconds * (2 ** min(attempt, 30)))
    return min(max_seconds, max(0.0, ceiling * (1 + jitter_ratio * (2 * sample - 1))))


class ConnectorRuntimeStore:
    """SQLite state for one connector ID; credentials and lease tokens are never stored."""

    def __init__(self, db_path: Path | str, connector_id: str) -> None:
        if not isinstance(connector_id, str) or not _CONNECTOR_ID.fullmatch(connector_id):
            raise ValueError("invalid connector identity")
        self.db_path = Path(db_path)
        self.connector_id = connector_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    def _migrate_schema(self) -> None:
        """Create the table and additive columns for stores created by earlier versions."""
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS connector_runtime_state (
                connector_id TEXT PRIMARY KEY, protocol_version TEXT, client_version TEXT,
                connection_state TEXT NOT NULL DEFAULT 'offline', last_server_time TEXT,
                last_heartbeat_id TEXT, last_heartbeat_at REAL,
                reconnect_attempt INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                last_error_code TEXT, last_error_at REAL,
                connection_epoch INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row["name"] for row in db.execute(
                "PRAGMA table_info(connector_runtime_state)"
            )}
            migrations = {
                "last_error_code": "ALTER TABLE connector_runtime_state ADD COLUMN last_error_code TEXT",
                "last_error_at": "ALTER TABLE connector_runtime_state ADD COLUMN last_error_at REAL",
                "connection_epoch": (
                    "ALTER TABLE connector_runtime_state ADD COLUMN "
                    "connection_epoch INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)
            db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _ensure_row(self, db: sqlite3.Connection) -> None:
        db.execute("""INSERT OR IGNORE INTO connector_runtime_state
            (connector_id, connection_state, updated_at) VALUES (?, 'offline', ?)""",
            (self.connector_id, time.time()))

    def record_handshake(self, protocol_version: str, client_version: str,
                         server_time: str | None = None) -> None:
        protocol = negotiate_protocol(protocol_version)
        if not isinstance(client_version, str) or not client_version or len(client_version) > 64:
            raise ValueError("invalid client version")
        now = time.time()
        with closing(self._connect()) as db, db:
            self._ensure_row(db)
            db.execute("""UPDATE connector_runtime_state SET protocol_version=?, client_version=?,
                connection_state='online', last_server_time=?, reconnect_attempt=0,
                last_error_code=NULL, last_error_at=NULL, last_heartbeat_id=NULL,
                last_heartbeat_at=NULL, connection_epoch=connection_epoch+1,
                updated_at=? WHERE connector_id=?""",
                (protocol, client_version, server_time, now, self.connector_id))

    def record_heartbeat(self, heartbeat_id: str, received_at: float | None = None) -> None:
        if not isinstance(heartbeat_id, str) or not _HEARTBEAT_ID.fullmatch(heartbeat_id):
            raise ValueError("invalid heartbeat ID")
        now = time.time() if received_at is None else float(received_at)
        if not math.isfinite(now):
            raise ValueError("heartbeat timestamp must be finite")
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT protocol_version FROM connector_runtime_state WHERE connector_id=?",
                (self.connector_id,),
            ).fetchone()
            if row is None or not row["protocol_version"]:
                raise ValueError("handshake required before recording heartbeat")
            db.execute("""UPDATE connector_runtime_state SET connection_state='online',
                last_heartbeat_id=?, last_heartbeat_at=?, reconnect_attempt=0,
                last_error_code=NULL, last_error_at=NULL, updated_at=?
                WHERE connector_id=?""",
                (heartbeat_id, now, time.time(), self.connector_id))

    def record_error(self, error_code: str) -> None:
        """Persist only a bounded error category, never raw exception text or credentials."""
        if not isinstance(error_code, str) or error_code not in CONNECTOR_ERROR_CODES:
            raise ValueError("unsupported connector error code")
        now = time.time()
        with closing(self._connect()) as db, db:
            self._ensure_row(db)
            db.execute("""UPDATE connector_runtime_state SET connection_state='degraded',
                last_error_code=?, last_error_at=?, updated_at=? WHERE connector_id=?""",
                (error_code, now, now, self.connector_id))

    def mark_disconnected(self) -> None:
        with closing(self._connect()) as db, db:
            self._ensure_row(db)
            db.execute("""UPDATE connector_runtime_state SET connection_state='offline',
                updated_at=? WHERE connector_id=?""", (time.time(), self.connector_id))

    def record_reconnect_attempt(self) -> int:
        """Atomically increment the durable attempt counter across concurrent callers."""
        with closing(self._connect()) as db, db:
            self._ensure_row(db)
            db.execute("""UPDATE connector_runtime_state
                SET reconnect_attempt=reconnect_attempt+1, updated_at=?
                WHERE connector_id=?""", (time.time(), self.connector_id))
            row = db.execute(
                "SELECT reconnect_attempt FROM connector_runtime_state WHERE connector_id=?",
                (self.connector_id,),
            ).fetchone()
            return int(row["reconnect_attempt"])

    def load(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM connector_runtime_state WHERE connector_id=?",
                             (self.connector_id,)).fetchone()
        if row is None:
            return {
                "connector_id": self.connector_id, "protocol_version": None,
                "client_version": None, "connection_state": "offline",
                "last_server_time": None, "last_heartbeat_id": None,
                "last_heartbeat_at": None, "reconnect_attempt": 0,
                "last_error_code": None, "last_error_at": None, "connection_epoch": 0,
            }
        return dict(row)

    def snapshot(self, *, now: float | None = None,
                 heartbeat_interval_seconds: int = 30,
                 presence_ttl_seconds: int = 90) -> dict[str, Any]:
        """Return a redacted, derived health snapshot without mutating persisted state."""
        state = self.load()
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time):
            raise ValueError("snapshot time must be finite")
        heartbeat_at = state["last_heartbeat_at"]
        age = None if heartbeat_at is None else max(0.0, current_time - float(heartbeat_at))
        health = classify_connection_age(
            age,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            presence_ttl_seconds=presence_ttl_seconds,
        )
        if state["connection_state"] == "offline":
            health["state"] = "offline"
            health["stale"] = True
        elif state["connection_state"] == "degraded" and health["state"] == "online":
            health["state"] = "degraded"
        return {
            "connector_id": self.connector_id,
            "connection_state": health["state"],
            "seconds_since_heartbeat": health["seconds_since_seen"],
            "stale": health["stale"],
            "protocol_version": state["protocol_version"],
            "client_version": state["client_version"],
            "last_heartbeat_id": state["last_heartbeat_id"],
            "reconnect_attempt": state["reconnect_attempt"],
            "last_error_code": state["last_error_code"],
            "last_error_at": state["last_error_at"],
            "connection_epoch": state["connection_epoch"],
        }
