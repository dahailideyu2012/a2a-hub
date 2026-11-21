# PostgreSQL Task Store V1 — Runtime Adapter

Status: **runtime adapter implemented on an isolated feature branch; fixed-SHA static review and real PostgreSQL 16 CI integration passed, including cancellation fencing, startup outage, and pool shutdown; production readiness remains pending final review and deployment-specific operational checks**.

The branch now includes a synchronous `PostgresTaskStore`, bounded psycopg connection pool, startup migration/schema-version check, explicit `A2A_POSTGRES_DSN` configuration, and opt-in integration tests. The adapter is selected only when `A2A_STORE=postgres`; missing DSN, missing optional driver, connection failure, or schema failure must stop startup. No automatic fallback to SQLite or memory is permitted.

WorkBuddy fixed-SHA static review for `3838d30de8bffa81a49ad05f2969ea77e152643e` passed: 714 passed, 5 PostgreSQL integration tests skipped, 0 failed. The dedicated GitHub Actions workflow then executed against an ephemeral PostgreSQL 16 service on fixed code SHA `94d148b7f32eb215fa7075f9a15942d0c306a8b9` (Run `44904518389`): focused PostgreSQL/repository tests **42 passed**; full suite **722 passed, 1 warning, 5 subtests passed, 0 failed**; compileall, loopback acceptance, wheel build, and packaged migration verification all passed. All eight opt-in PostgreSQL integration tests ran against the real disposable service, covering lifecycle/idempotent replay, two-process concurrent claim, lease-reclaim fencing, idempotency conflict rollback, CRUD, unreachable-database startup failure, closed-pool rejection, and cancellation fencing. The PostgreSQL service log contains no `there is already a transaction in progress` warning after the migration autocommit fix. The only pytest warning is the existing StarletteDeprecationWarning for httpx with starlette.testclient; GitHub Actions also emitted a Node.js 20 deprecation warning from actions/checkout@v4 and actions/setup-python@v5. WorkBuddy's local environment may still skip local PG tests; the CI run is the actual database evidence. Do not enable it in production until final fixed-SHA WorkBuddy review and deployment-specific checks are complete.

## Schema contract

- `tasks` preserves the current serialized Task payload and all Pull queue / lease-fencing fields used by the SQLite reference.
- `task_idempotency` binds one operation ID to one request hash and one task ID. Its foreign key prevents a mapping from pointing at a task that was never committed.
- `lease_version` and `last_progress_seq` are non-negative; execution mode is restricted to `hub` or `pull`.
- Partial indexes target the Pull queue and active leases; idempotency expiry has its own index.
- `a2a_schema_migrations` records schema version 1. The migration is wrapped in a transaction and uses idempotent DDL / version insertion.

## Implemented adapter transaction patterns

The adapter claims work in one database transaction using a CTE + `UPDATE ... RETURNING`; PostgreSQL row locks, not process-local locks, coordinate independent Hub processes. The claim query orders the locking clause after `LIMIT`:

```sql
BEGIN;
SELECT id
FROM tasks
WHERE execution_mode = 'pull'
  AND state IN ('submitted', 'working')
  AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= :now)
ORDER BY created ASC, id ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
-- Update the selected row's connector_id, fresh lease_token,
-- lease_version = lease_version + 1, lease_expires_at and lease_state.
COMMIT;
```

The implementation uses a single CTE + `UPDATE ... RETURNING`, so selection and lease assignment are one statement. Every lease mutation compares connector ID, token, version, and unexpired lease in the update predicate. Result replay preserves the exact-result rule for the original lease and payload.

Task creation and idempotency binding must occur in the **same PostgreSQL transaction**. On an operation ID conflict, compare request hash and task ID; never overwrite a different binding. A retry with the same key and hash must return the original task.

## Remaining acceptance gates before enabling the backend

1. WorkBuddy must complete fixed-SHA source review, archive/blob verification, repository contract tests, full pytest, compileall, and loopback regression.
2. Run the opt-in integration suite against a disposable PostgreSQL instance, including migration execution and schema validation.
3. Verify two independent OS processes race to claim tasks without duplicate delivery.
4. Verify lease expiry/reclaim fences every stale-worker mutation, exact result replay, idempotency conflict handling, and cancellation behavior.
5. Exercise database outage/startup failure and pool shutdown behavior in an isolated environment.
6. Keep the factory fail-closed: no fallback to memory or SQLite on connection, schema, or configuration errors.
7. Only after all acceptance gates pass may PostgreSQL be enabled for production; migration/deployment still requires separate explicit approval.

## Current status

The memory and SQLite backends remain supported. PostgreSQL runtime code is present on this feature branch but remains **provisional** until fixed-SHA acceptance and real PostgreSQL integration tests pass. Set `A2A_STORE=postgres`, `A2A_POSTGRES_DSN`, and optionally `A2A_POSTGRES_POOL_MIN_SIZE`, `A2A_POSTGRES_POOL_MAX_SIZE`, and `A2A_POSTGRES_CONNECT_TIMEOUT`. Install with `pip install -e ".[postgres,dev]"`.

The integration tests in `tests/test_postgres_store.py` are opt-in via `A2A_TEST_POSTGRES_DSN` and must target a disposable database. Without that variable they skip and do not open network connections. WorkBuddy must report skipped integration tests as skipped, not passed. The dedicated GitHub Actions workflow supplies an ephemeral PostgreSQL 16 database through a service container; it does not use production credentials or infrastructure. Run `44904518389` passed on code SHA `94d148b7f32eb215fa7075f9a15942d0c306a8b9`; subsequent source changes require a fresh run.
