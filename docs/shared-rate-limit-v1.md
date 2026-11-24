# T33-A: shared WorkBuddy rate limiting

The WorkBuddy connector uses a SQLite-backed sliding-window store. Every worker
that serves the same connector deployment must use the same durable
`A2A_WORKBUDDY_RATE_LIMIT_DB_PATH` file on storage that supports SQLite locking.

## Behavior

- All ten WorkBuddy connector endpoints share one bucket per connector identity,
  credential, and configured limit; endpoint paths do not create separate budgets.
- The bucket key is SHA-256 hashed before persistence. Raw connector credentials
  are not written to the rate-limit database.
- Each hit uses a short-lived SQLite connection and `BEGIN IMMEDIATE` around
  cleanup, count, and insert. This makes the check-and-consume operation atomic
  across processes sharing the database file.
- The store is called via `asyncio.to_thread` so SQLite work does not block the
  async request loop.
- Storage errors fail closed with HTTP 503. A rejected budget returns HTTP 429.
- A persisted per-bucket time high-water mark prevents a wall-clock rollback
  from granting extra quota. A forward clock jump can expire a window early;
  keep host clocks synchronized.
- Expired rows are removed opportunistically for the active bucket. The store
  also exposes `purge_expired()` for scheduled maintenance; no background
  cleanup worker is started by this change.

## Configuration

`A2A_WORKBUDDY_CONNECTOR_RATE_LIMIT=60` sets the shared 60-second budget.
`A2A_WORKBUDDY_RATE_LIMIT_DB_PATH=./data/workbuddy_rate_limit.db` selects the
shared SQLite file. All workers must resolve this setting to the same file.
Do not place this database on a filesystem without reliable SQLite locking.

## Scope and limitations

This slice changes WorkBuddy connector limiting only. ChatGPT ingress retains
its existing per-process limiter and is not claimed to be multi-worker safe.
PostgreSQL TaskStore, D1, Redis, and real WorkBuddy Desktop integration are out
of scope. The multi-process test uses local subprocesses and does not establish
anything about a production deployment or network filesystem behavior.

## Verification

```bash
pytest -q tests/test_rate_limit_store.py tests/test_workbuddy_rate_limit_shared.py
pytest -q tests/test_workbuddy_connector_security.py tests/test_workbuddy_connector.py
pytest -q
python -m compileall -q a2a_hub tests scripts run.py workbuddy_mcp_server.py
```
