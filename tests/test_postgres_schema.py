"""Static guardrails for the PostgreSQL v1 schema migration.

These checks do not replace running the migration against PostgreSQL. They make
critical columns, constraints, and indexes reviewable without a database service.
"""

from pathlib import Path


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "postgres"
    / "001_initial.sql"
)


def test_postgres_schema_contains_task_and_idempotency_records():
    sql = SCHEMA.read_text(encoding="utf-8").lower()
    assert "create table if not exists tasks" in sql
    assert "create table if not exists task_idempotency" in sql
    assert "payload text not null" in sql
    assert "operation_id text primary key" in sql
    assert "request_hash text not null" in sql
    assert "task_id text not null unique" in sql


def test_postgres_schema_preserves_pull_lease_fencing_fields():
    sql = SCHEMA.read_text(encoding="utf-8").lower()
    for field in (
        "execution_mode",
        "connector_id",
        "lease_token",
        "lease_version",
        "lease_expires_at",
        "lease_state",
        "last_progress_seq",
        "last_progress_hash",
        "result_key",
        "result_hash",
    ):
        assert field in sql
    assert "check (lease_version >= 0)" in sql
    assert "check (last_progress_seq >= 0)" in sql


def test_postgres_schema_has_queue_and_expiry_indexes():
    sql = SCHEMA.read_text(encoding="utf-8").lower()
    assert "idx_tasks_pull_queue" in sql
    assert "where execution_mode = 'pull'" in sql
    assert "idx_tasks_active_lease" in sql
    assert "idx_task_idempotency_expiry" in sql


def test_postgres_schema_migration_is_transactional_and_versioned():
    sql = SCHEMA.read_text(encoding="utf-8").lower()
    assert sql.strip().startswith("begin;")
    assert sql.strip().endswith("commit;")
    assert "create table if not exists a2a_schema_migrations" in sql
    assert "values (1)" in sql
    assert "on conflict (version) do nothing" in sql
