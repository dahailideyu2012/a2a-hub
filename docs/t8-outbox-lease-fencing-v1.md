# T8 — Durable callback outbox lease fencing

## Goal

T8 hardens the T6 SQLite callback outbox against a stale worker completing after
its lease has expired and another worker has reclaimed the same callback.

## Failure mode addressed

A lease expiry alone does not stop the old worker process from returning later.
Before T8, an old worker could still call `mark_delivered` or `mark_failed`
using only the task ID. If a replacement worker had already claimed the row,
the old worker could overwrite the replacement worker's state.

## Implementation

- Every successful `claim_due()` creates a fresh cryptographically random
  `lease_token` and persists it with the processing lease.
- Acknowledgement and failure updates require the exact current token, the
  expected attempt number (for failure), and an unexpired lease.
- Stale, expired, or superseded workers receive `False` from the state update;
  they cannot mark a newer attempt delivered, schedule its retry, or dead-letter it.
- The token is cleared when a lease is acknowledged, failed, manually retried,
  or dead-lettered. It is never returned by the public status endpoint.
- Existing T6 databases are migrated in place by adding the nullable
  `lease_token` column. Existing outbox rows and their status are preserved.
- Delivery remains at-least-once. The deterministic GitHub comment marker is
  still required for the ambiguous case where GitHub accepts a POST but the
  Hub loses the response.

## Validation

Focused coverage includes:

- A stale worker cannot acknowledge or fail a callback after another worker
  reclaims it.
- Concurrent claims against one SQLite file produce exactly one winner.
- A database using the pre-T8 T6 schema is migrated without losing its pending row.
- Existing retry, backoff, dead-letter, manual retry, and worker delivery tests
  continue to exercise the callback lifecycle.

Run:

```bash
python -m pytest -q tests/test_github_callback_outbox.py
python -m pytest -q
python -m compileall -q a2a_hub scripts tests
```

This stage uses synthetic/local tests only. It does not call GitHub, require
real credentials, merge the PR, or deploy the Hub.
