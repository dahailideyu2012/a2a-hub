# PostgreSQL Task Store V1 — Schema Slice

Status: **schema groundwork only; not a usable runtime backend**.

This slice adds `migrations/postgres/001_initial.sql` and static guardrail tests. It does not add a driver, connection pool, repository adapter, migration runner, PostgreSQL configuration, or production support. `A2A_STORE=postgres` must continue to fail closed.

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

The existing memory and SQLite backends remain the only runtime backends. This schema-only commit is not evidence of PostgreSQL syntax execution or multi-instance correctness; WorkBuddy must report that limitation explicitly.
