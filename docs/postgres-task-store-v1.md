# PostgreSQL Task Store V1 — Schema Slice

Status: **runtime adapter implemented on an isolated feature branch; not yet accepted or production-ready**.

The branch now includes a synchronous `PostgresTaskStore`, bounded psycopg connection pool, startup migration/schema-version check, explicit `A2A_POSTGRES_DSN` configuration, and opt-in integration tests. The adapter is selected only when `A2A_STORE=postgres`; missing DSN, missing optional driver, connection failure, or schema failure must stop startup. No automatic fallback to SQLite or memory is permitted.

The adapter code has not yet passed fixed-SHA WorkBuddy review or execution against a real PostgreSQL instance. Do not enable it in production.

## Schema contract

- `tasks` preserves the current serialized Task payload and all Pull queue / lease-fencing fields used by the SQLite reference.
- `task_idempotency` binds one operation ID to one request hash and one task ID. Its foreign key prevents a mapping from pointing at a task that was never committed.
- `lease_version` and `last_progress_seq` are non-negative; execution mode is restricted to `hub` or `pull`.
- Partial indexes target the Pull queue and active leases; idempotency expiry has its own index.
- `a2a_schema_migrations` records schema version 1. The migration is wrapped in a transaction and uses idempotent DDL / version insertion.

## Required adapter transaction patterns

The future adapter must claim work in one database transaction, not with a process lock. The intended pattern is:

```sql
BEGIN;
SELECT id
FROM tasks
WHERE execution_mode = 'pull'
  AND state IN ('submitted', 'working')
  AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= :now)
ORDER BY created ASC, id ASC
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- Update the selected row's connector_id, fresh lease_token,
-- lease_version = lease_version + 1, lease_expires_at and lease_state.
COMMIT;
```

The implementation should use a single CTE + `UPDATE ... RETURNING` where practical, so selection and lease assignment are one statement. Every lease mutation must compare connector ID, token, version, and unexpired lease in the update predicate. Result replay must preserve the existing exact-result rule.

Task creation and idempotency binding must occur in the **same PostgreSQL transaction**. On an operation ID conflict, compare request hash and task ID; never overwrite a different binding. A retry with the same key and hash must return the original task.

## Acceptance gates before enabling the backend

1. Implement a synchronous adapter matching `TaskRepository` and `PullTaskRepository` (the current Hub call sites are synchronous).
2. Add an optional PostgreSQL driver dependency; importing the application without that extra must continue to work for memory/SQLite users.
3. Add connection-pool lifecycle, bounded connect/statement timeouts, startup connectivity + schema validation, and deterministic close behavior.
4. Run the shared repository contract suite against PostgreSQL, including two independent processes racing to claim tasks.
5. Add failure-injection tests for idempotency, lease expiry/reclaim, stale-worker fencing, cancellation races, and database outages.
6. Keep the factory fail-closed: no fallback to memory or SQLite on connection, schema, or configuration errors.
7. Only after fixed-SHA acceptance may PostgreSQL be described as implemented. Production migration/deployment still requires separate explicit approval.

## Current status

The memory and SQLite backends remain supported. PostgreSQL runtime code is present on this feature branch but is **provisional** until fixed-SHA acceptance. Set `A2A_STORE=postgres`, `A2A_POSTGRES_DSN`, and optionally `A2A_POSTGRES_POOL_MIN_SIZE`, `A2A_POSTGRES_POOL_MAX_SIZE`, and `A2A_POSTGRES_CONNECT_TIMEOUT`. Install with `pip install -e ".[postgres,dev]"`.

The integration tests in `tests/test_postgres_store.py` are opt-in via `A2A_TEST_POSTGRES_DSN` and must target a disposable database. Without that variable they skip and do not open network connections. WorkBuddy must report skipped integration tests as skipped, not passed. A real PostgreSQL instance is required to establish SQL/runtime and multi-process concurrency correctness.
