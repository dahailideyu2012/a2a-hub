# WorkBuddy persisted connection events and diagnostics (T99–T105)

## Scope

This batch closes the T92–T98 observability gap by persisting only bounded connection
event metadata in the existing WorkBuddy presence SQLite database. It does not store
raw exceptions, stack traces, credentials, lease tokens, task payloads or user content.

## Authenticated event reporting

- `POST /integrations/workbuddy/v1/connection-events/error`
  accepts `connector_id` and one allowlisted `error_code`: `network_unreachable`,
  `timeout`, `authentication_failed`, `protocol_mismatch`, `hub_unavailable`,
  or `unknown`. Unknown fields and raw error strings are rejected.
- `POST /integrations/workbuddy/v1/connection-events/reconnect-attempt`
  atomically increments the durable retry-attempt counter for the configured connector.

Both endpoints require the dedicated WorkBuddy connector token, pass through the existing
rate limiter and audit decorator, and enforce the configured connector identity.

## Diagnostics

Authenticated diagnostics now reports `last_error_code`, `last_error_at` and
`reconnect_attempt` from the durable event table. Recovery advice therefore reflects
fatal authentication/protocol failures and retry-budget exhaustion on the real HTTP
data path, including before the first handshake. The diagnostics envelope version was 3 and is extended to version 4 by T106–T112;
all previous fields remain additive and backward compatible.

A successful handshake or accepted heartbeat clears the previous error and retry
history. Duplicate/stale heartbeats do not clear state because they are rejected before
the event reset is committed.

## Safety and limits

Recovery remains advisory. `automatic_actions_enabled` is always false; no network
reconnect, heartbeat probe, task claim, task execution or background worker is started.
The API accepts categories only, never arbitrary exception text. Event rows contain
only connector identity, an allowlisted code, timestamp and integer retry counter.


## Operator recovery decision receipt (T106–T112)

Diagnostics v4 includes a deterministic `recovery_snapshot_id`. The ID is derived
only from redacted connection state, bounded error/retry metadata, and a monotonic
recovery revision; it contains no credential or task payload.

An authenticated connector may POST `/recovery/decision` with the current
snapshot ID and one of `acknowledged`, `deferred`, or `dismissed`. The Hub
persists the operator decision using a revision-checked SQLite transaction.
An exact retry is idempotent; a conflicting decision for the same snapshot or a
stale snapshot returns HTTP 409. A new handshake, accepted heartbeat, reported
error, or reconnect attempt advances the revision and invalidates the prior
decision. Diagnostics exposes the decision only while it still matches the
current snapshot.

The receipt means only that the authenticated connector recorded an operator
decision. It does not prove that a human actually performed the recommended
action, and it never performs that action. `action_executed` and
`automatic_actions_enabled` remain false. No reconnect, heartbeat probe,
task claim, task execution, or background worker is started.
