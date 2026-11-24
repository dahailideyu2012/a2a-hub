"""SQLite-backed, cross-process sliding-window rate-limit storage."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class SQLiteRateLimitStore:
    """Atomic sliding-window counter shared by processes using the same SQLite file.

    Each call owns a short-lived connection and transaction. BEGIN IMMEDIATE
    serializes the count/insert pair across processes. Storage errors propagate
    so callers can fail closed rather than silently bypassing the limit.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rate_limit_hits (
                bucket_key TEXT NOT NULL,
                hit_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_bucket_time "
            "ON rate_limit_hits(bucket_key, hit_at)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rate_limit_clock (
                bucket_key TEXT PRIMARY KEY,
                last_now REAL NOT NULL
            )"""
        )
        return conn

    def allow(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> bool:
        """Return whether this hit fits the bucket's rolling window.

        A persisted per-bucket high-water mark prevents wall-clock rollback
        from creating extra quota across workers. A forward clock jump can
        expire a window early; deployments should keep host clocks synchronized.
        """
        if not key:
            raise ValueError("rate-limit key must not be empty")
        limit = max(1, int(limit))
        window = max(1, int(window_seconds))
        sampled_now = time.time() if now is None else float(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_now FROM rate_limit_clock WHERE bucket_key=?", (key,)
            ).fetchone()
            effective_now = max(sampled_now, float(row[0])) if row else sampled_now
            conn.execute(
                """INSERT INTO rate_limit_clock(bucket_key, last_now) VALUES(?, ?)
                   ON CONFLICT(bucket_key) DO UPDATE SET last_now=excluded.last_now""",
                (key, effective_now),
            )
            cutoff = effective_now - window
            # Opportunistic bucket-local cleanup keeps the hot path bounded.
            conn.execute(
                "DELETE FROM rate_limit_hits WHERE bucket_key=? AND hit_at<=?",
                (key, cutoff),
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_hits WHERE bucket_key=? AND hit_at>?",
                (key, cutoff),
            ).fetchone()[0]
            if int(count) >= limit:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO rate_limit_hits(bucket_key, hit_at) VALUES(?, ?)",
                (key, effective_now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def purge_expired(self, *, now: float | None = None, window_seconds: int = 60) -> int:
        """Remove expired hit rows and stale bucket clocks; safe to run periodically."""
        sampled_now = time.time() if now is None else float(now)
        cutoff = sampled_now - max(1, int(window_seconds))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM rate_limit_hits WHERE hit_at<=?", (cutoff,))
            removed = int(cursor.rowcount)
            conn.execute(
                """DELETE FROM rate_limit_clock
                   WHERE last_now<=? AND NOT EXISTS
                     (SELECT 1 FROM rate_limit_hits h
                      WHERE h.bucket_key=rate_limit_clock.bucket_key)""",
                (cutoff,),
            )
            conn.commit()
            return removed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
