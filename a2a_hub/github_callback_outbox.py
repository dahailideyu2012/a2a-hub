"""Durable SQLite outbox for GitHub Issue result callbacks."""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .github_result_callback import deliver_github_issue_result


class GithubCallbackOutbox:
    """Lease-based outbox safe across restarts and concurrent workers.

    Each claim receives a fresh fencing token. A worker whose lease has been
    reclaimed cannot acknowledge or fail the newer worker's delivery attempt.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_attempts: int = 12,
        lease_seconds: int = 60,
        base_backoff: int = 5,
        max_backoff: int = 3600,
    ) -> None:
        self.path = Path(path)
        self.max_attempts = max(1, int(max_attempts))
        self.lease_seconds = max(15, int(lease_seconds))
        self.base_backoff = max(1, int(base_backoff))
        self.max_backoff = max(self.base_backoff, int(max_backoff))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS github_callback_outbox (
                    task_id TEXT PRIMARY KEY, callback_json TEXT NOT NULL,
                    state TEXT NOT NULL, output TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','delivered','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
                    lease_until REAL, lease_token TEXT, last_error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )"""
            )
            # Online migration for databases created by T6 before fencing tokens.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(github_callback_outbox)")
            }
            if "lease_token" not in columns:
                conn.execute("ALTER TABLE github_callback_outbox ADD COLUMN lease_token TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_github_callback_due "
                "ON github_callback_outbox(status,next_attempt_at,lease_until)"
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    def enqueue(self, task_id: str, callback: dict[str, Any], state: str, output: str) -> bool:
        if state not in {"completed", "failed"}:
            raise ValueError("callback state must be terminal")
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO github_callback_outbox
                   (task_id,callback_json,state,output,status,attempts,next_attempt_at,
                    created_at,updated_at)
                   VALUES(?,?,?,?,'pending',0,?,?,?)""",
                (
                    task_id,
                    json.dumps(callback, ensure_ascii=False, sort_keys=True),
                    state,
                    output,
                    now,
                    now,
                    now,
                ),
            )
            return cur.rowcount == 1

    def claim_due(self) -> dict[str, Any] | None:
        now = time.time()
        lease_token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE github_callback_outbox
                   SET status='dead',lease_until=NULL,lease_token=NULL,updated_at=?
                   WHERE status='processing' AND lease_until<=? AND attempts>=?""",
                (now, now, self.max_attempts),
            )
            row = conn.execute(
                """SELECT * FROM github_callback_outbox
                   WHERE (status='pending' AND next_attempt_at<=?)
                      OR (status='processing' AND lease_until<=?)
                   ORDER BY next_attempt_at,created_at LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cur = conn.execute(
                """UPDATE github_callback_outbox
                   SET status='processing',attempts=attempts+1,lease_until=?,
                       lease_token=?,updated_at=?
                   WHERE task_id=? AND
                     ((status='pending' AND next_attempt_at<=?)
                      OR (status='processing' AND lease_until<=?))""",
                (now + self.lease_seconds, lease_token, now, row["task_id"], now, now),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return {
                "task_id": row["task_id"],
                "callback": json.loads(row["callback_json"]),
                "state": row["state"],
                "output": row["output"],
                "attempts": int(row["attempts"]) + 1,
                "lease_token": lease_token,
            }

    def mark_delivered(self, task_id: str, lease_token: str) -> bool:
        """Acknowledge only the currently-owned lease; stale workers are ignored."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE github_callback_outbox
                   SET status='delivered',lease_until=NULL,lease_token=NULL,
                       last_error=NULL,updated_at=?
                   WHERE task_id=? AND status='processing' AND lease_token=? AND lease_until>?""",
                (time.time(), task_id, lease_token, time.time()),
            )
            return cur.rowcount == 1

    def mark_failed(
        self, task_id: str, attempts: int, lease_token: str, error: Exception
    ) -> bool:
        """Schedule retry only if this worker still owns the current lease."""
        now = time.time()
        dead = attempts >= self.max_attempts
        delay = min(
            self.max_backoff,
            self.base_backoff * (2 ** min(max(attempts - 1, 0), 20)),
        )
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE github_callback_outbox
                   SET status=?,next_attempt_at=?,lease_until=NULL,lease_token=NULL,
                       last_error=?,updated_at=?
                   WHERE task_id=? AND status='processing' AND lease_token=? AND attempts=? AND lease_until>?""",
                (
                    "dead" if dead else "pending",
                    now + delay,
                    type(error).__name__[:80],
                    now,
                    task_id,
                    lease_token,
                    attempts,
                    now,
                ),
            )
            return cur.rowcount == 1

    def retry(self, task_id: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE github_callback_outbox
                   SET status='pending',attempts=0,next_attempt_at=?,
                       lease_until=NULL,lease_token=NULL,last_error=NULL,updated_at=?
                   WHERE task_id=? AND status IN ('dead','pending')""",
                (now, now, task_id),
            )
            return cur.rowcount == 1

    def status(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT task_id,status,attempts,next_attempt_at,last_error,created_at,updated_at
                   FROM github_callback_outbox WHERE task_id=?""",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def recover_tasks(self, tasks: list[Any]) -> int:
        recovered = 0
        for task in tasks:
            callback = getattr(task, "metadata", {}).get("github_issue_callback")
            state = getattr(getattr(getattr(task, "status", None), "state", None), "value", None)
            if isinstance(callback, dict) and state in {"completed", "failed"}:
                output = str(task.final_text() or "")
                if not output and getattr(task, "history", None):
                    output = task.history[-1].text()
                recovered += int(self.enqueue(str(task.id), callback, state, output))
        return recovered


async def run_outbox_worker(
    outbox: GithubCallbackOutbox,
    stop: asyncio.Event,
    *,
    api_token: str,
    allowed_repos: str,
    poll_seconds: float = 2.0,
    deliver: Callable[..., dict[str, Any]] = deliver_github_issue_result,
) -> None:
    while not stop.is_set():
        item = await asyncio.to_thread(outbox.claim_due)
        if item is not None:
            try:
                await asyncio.to_thread(
                    deliver,
                    item["callback"],
                    item["task_id"],
                    item["state"],
                    item["output"],
                    api_token,
                    allowed_repos,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    outbox.mark_failed,
                    item["task_id"],
                    item["attempts"],
                    item["lease_token"],
                    exc,
                )
            else:
                await asyncio.to_thread(
                    outbox.mark_delivered, item["task_id"], item["lease_token"]
                )
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.1, poll_seconds))
        except asyncio.TimeoutError:
            pass
