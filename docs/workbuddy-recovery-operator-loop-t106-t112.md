# WorkBuddy recovery operator loop (T106–T112)

## Goal

Provide a safe, explicit operator-decision receipt around the existing bounded
recovery advice. The Hub can explain whether the connector is healthy, degraded,
offline, retryable, or requires operator intervention. This increment lets an
authenticated connector record that the current advice was acknowledged,
deferred, or dismissed without claiming that any recovery action actually ran.

## Task breakdown

- **T106 — Stable recovery snapshot ID:** derive a deterministic SHA-256 ID from
  a closed set of redacted diagnostics fields and a monotonic recovery revision.
- **T107 — Durable decision state:** add an additive SQLite migration for the
  recovery revision and last operator decision; preserve existing error/retry
  records and make state changes invalidate prior decisions.
- **T108 — Authenticated decision API:** add
  `POST /integrations/workbuddy/v1/recovery/decision`, with dedicated token
  authentication, configured identity binding, strict payload validation,
  existing rate limiting, and HTTP 409 for stale/conflicting decisions.
- **T109 — Diagnostics v4:** expose the snapshot ID and only the decision that
  matches the current snapshot; retain all v3 fields.
- **T110 — MCP read/receipt tools:** add
  `workbuddy_recovery_diagnostics` and
  `workbuddy_record_recovery_decision`. Diagnostics is deliberately read-only
  and does not handshake, because handshake clears previous error/retry history.
- **T111 — Regression/security coverage:** test additive migration, revision
  compare-and-set, idempotent replay, concurrent conflicting decisions, auth,
  identity, strict schema, stale snapshots, rate limiting, redaction, and MCP
  response validation.
- **T112 — Protocol/security documentation:** document the operator workflow
  and its non-automation boundary.

## API contract

Read the current diagnostics:

`GET /integrations/workbuddy/v1/diagnostics`

The v4 response includes `recovery_snapshot_id` and `operator_decision`.
The snapshot ID changes when the recovery state changes, including a new
handshake, accepted heartbeat, reported error, reconnect attempt, or TTL-derived
state transition.

Record a user/operator disposition:

`POST /integrations/workbuddy/v1/recovery/decision`

```json
{
  "connector_id": "workbuddy-personal",
  "snapshot_id": "<current recovery_snapshot_id>",
  "disposition": "acknowledged"
}
```

Allowed dispositions: `acknowledged`, `deferred`, `dismissed`.

- Exact replay of the same disposition for the same snapshot returns the
  original receipt timestamp.
- A different disposition for the same snapshot returns HTTP 409.
- A state revision change between diagnostics and write returns HTTP 409.
- Unknown fields, invalid IDs, invalid dispositions, missing credentials, and
  identity mismatches fail closed.
- A subsequent state mutation invalidates the prior decision.

## MCP usage

1. Call `workbuddy_recovery_diagnostics` to read the current advice and snapshot
   ID. This tool performs no handshake, heartbeat, reconnect, or task request.
2. Only after explicit user/operator direction, call
   `workbuddy_record_recovery_decision` with that exact snapshot ID and
   disposition.
3. On HTTP 409, read diagnostics again; do not retry with the stale ID.

## Security and non-goals

- The only authentication credential is the existing dedicated WorkBuddy
  Connector Token; it is never returned or logged.
- Connector identity is checked against server configuration.
- The request schema is closed; no free-form notes or raw exception strings are
  accepted.
- Audit records use the closed event schema and token fingerprint only.
- SQLite writes use a monotonic revision and transactional compare-and-set.
- A decision receipt is not proof of a human action and does not report an
  action as completed.
- `action_executed=false` and `automatic_actions_enabled=false` are fixed
  response values.
- No reconnect, heartbeat probe, background loop, task claim, task execution,
  production access, real Desktop configuration, or credential handling is
  introduced by this increment.
- Automated tests use isolated temporary databases and mocked/local HTTP
  surfaces; they do not prove a real WorkBuddy Desktop connection.

## Acceptance commands

```bash
pytest -q tests/test_workbuddy_recovery_decision_store.py tests/test_workbuddy_recovery_decision_api.py tests/test_workbuddy_connection_events_api.py tests/test_workbuddy_mcp_server.py tests/test_workbuddy_runtime.py
python -m compileall -q a2a_hub scripts tests run.py workbuddy_mcp_server.py
pytest -q
```

Independent acceptance must use a fresh fixed-SHA archive and isolated virtual
environment, verify exact PR HEAD and CI, report command exit codes and test
counts, inspect the two destroy-confirmation tests from PR #29 remain unchanged,
and verify no production, real credentials, real Desktop, or real
`claim_task` activity occurred.
