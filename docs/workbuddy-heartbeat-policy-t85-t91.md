# Configurable WorkBuddy heartbeat policy (T85–T91)

## Purpose

Remove the server's implicit 30-second heartbeat assumption so the connector
and Hub can agree on one configured cadence. The server remains pull-based;
this change does not create a polling daemon or start task execution.

## Configuration

- Environment variable: `A2A_WORKBUDDY_HEARTBEAT_INTERVAL_SECONDS`
- Setting: `workbuddy_heartbeat_interval_seconds`
- Default: 30 seconds (backward compatible)
- Allowed range: 5–300 seconds
- Presence TTL remains `A2A_WORKBUDDY_PRESENCE_TTL_SECONDS` (30–3600 seconds)
- TTL must be greater than or equal to the heartbeat interval. Invalid
  interval/TTL combinations fail closed with HTTP 503 rather than returning
  contradictory liveness guidance.

## API contract

- T85: introduce the configurable interval with the legacy default.
- T86: validate interval and TTL together at the WorkBuddy API boundary.
- T87: handshake advertises the configured heartbeat interval.
- T88: heartbeat receipt returns the configured next-heartbeat interval.
- T89: diagnostics uses the same interval for online/degraded/offline
  classification and reports the configured value.
- T90: API tests cover defaults/overrides, lower/upper bounds, invalid TTL
  relationships, handshake/heartbeat/diagnostics consistency.
- T91: document the environment variable, bounds, compatibility and safety.

## Compatibility and safety

Existing installations retain 30 seconds without configuration changes.
Presence TTL behavior remains bounded and explicit. A configuration error
cannot silently produce a heartbeat interval longer than its TTL. No new
credentials, permissions, task operations, autonomous polling, or production
deployment are introduced.
