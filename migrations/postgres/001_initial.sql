-- PostgreSQL task-store schema v1.
-- Runtime adapter is provisional until fixed-SHA review and real PostgreSQL acceptance.
BEGIN;

CREATE TABLE IF NOT EXISTS a2a_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    state TEXT,
    created TEXT,
    updated TEXT,
    payload TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'hub'
        CHECK (execution_mode IN ('hub', 'pull')),
    connector_id TEXT,
    lease_token TEXT,
    lease_version BIGINT NOT NULL DEFAULT 0 CHECK (lease_version >= 0),
    lease_expires_at DOUBLE PRECISION,
    lease_state TEXT,
    last_progress_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_progress_seq >= 0),
    last_progress_hash TEXT,
    result_key TEXT,
    result_hash TEXT,
    CHECK (
        (lease_token IS NULL AND connector_id IS NULL)
        OR (lease_token IS NOT NULL AND connector_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tasks_updated
    ON tasks (updated DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_pull_queue
    ON tasks (created ASC, id ASC)
    WHERE execution_mode = 'pull'
      AND state IN ('submitted', 'working');

CREATE INDEX IF NOT EXISTS idx_tasks_active_lease
    ON tasks (lease_expires_at)
    WHERE execution_mode = 'pull'
      AND lease_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_idempotency (
    operation_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    CONSTRAINT fk_task_idempotency_task
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
    CHECK (expires_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_task_idempotency_expiry
    ON task_idempotency (expires_at);

INSERT INTO a2a_schema_migrations(version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;

COMMIT;
