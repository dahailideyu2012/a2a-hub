# WorkBuddy Connector Protocol v1.0

This document describes the authenticated bootstrap and liveness receipts added for
the outbound-poll WorkBuddy connector. It does not install or configure WorkBuddy
Desktop, and a successful HTTP handshake is not proof that a Desktop process is
running.

## Authentication and identity

All requests use the dedicated connector credential:

```http
X-WorkBuddy-Connector-Token: <A2A_WORKBUDDY_CONNECTOR_TOKEN>
```

Do not reuse the ChatGPT ingress token or the general Hub API token. The Hub
compares the token in constant time and separately checks the configured
`A2A_WORKBUDDY_CONNECTOR_ID`. The connector identity is configuration-bound;
clients cannot register arbitrary identities through the handshake.

The API is under `/integrations/workbuddy/v1`. Use HTTPS outside the loopback-only
acceptance sandbox.

## Handshake

`POST /handshake`

Request:

```json
{
  "connector_id": "workbuddy-personal",
  "protocol_version": "1.0",
  "client_version": "5.11.3.0",
  "capabilities": ["claim", "start", "renew", "progress", "result", "heartbeat"]
}
```

The server rejects an invalid token (401), identity mismatch (403), an unconfigured
agent allowlist (503), unsupported protocol versions or unknown fields (422).
The response includes the configured agent allowlist, negotiated capabilities,
lease duration, heartbeat interval, and server time. It never returns credentials
or lease tokens.

## Heartbeat receipt

`POST /heartbeat`

Request:

```json
{
  "connector_id": "workbuddy-personal",
  "heartbeat_id": "heartbeat-0001",
  "status": "idle",
  "current_task_id": null
}
```

Allowed status values are `idle`, `busy`, and `degraded`. The response echoes
the caller's heartbeat ID as a receipt ID and returns server time and the next
heartbeat interval. Treat the receipt as proof that this authenticated request
was accepted—not as proof of task completion or durable presence history.

## Task execution lifecycle

After handshake, the connector uses the existing endpoints:

1. `POST /claim` to claim one task and receive a fenced lease.
2. `POST /tasks/{task_id}/start` to mark execution started.
3. `POST /tasks/{task_id}/renew` to renew the lease while working.
4. `POST /tasks/{task_id}/progress` with strictly increasing sequence numbers.
5. `POST /tasks/{task_id}/result` with a stable result key and terminal state.

Every mutation must include the lease token and version returned by claim.
A stale lease is rejected. Keep lease tokens local and never include them in
logs, prompts, screenshots, or ChatGPT messages.

## Current limitation

Handshake and heartbeat update a durable, single-connector presence record as
described below; they do not launch a WorkBuddy process or prove a Desktop UI
session. Real Desktop integration still requires a supported client/connector to
call these endpoints and a separately authorized sandbox round-trip. Automated
tests use isolated temporary stores and do not claim real Desktop acceptance.


## Durable presence registry (T17)

The Hub persists the most recent authenticated handshake and heartbeat in a
separate SQLite database. Configure:

- `A2A_WORKBUDDY_PRESENCE_DB_PATH` (default: `./data/workbuddy_presence.db`)
- `A2A_WORKBUDDY_PRESENCE_TTL_SECONDS` (default: `90`, allowed: 30–3600)

The heartbeat interval remains 30 seconds. The default 90-second TTL tolerates
short network interruptions while allowing the Hub to report a connector offline
after missed heartbeats. No background worker is required: online/offline is
derived at read time from `last_seen_at`.

`GET /integrations/workbuddy/v1/presence` requires the dedicated connector token.
It returns the configured connector's online flag, effective status, client and
protocol versions, negotiated capabilities, last heartbeat receipt, last-seen
timestamp, and TTL. When the TTL expires, `online=false`, `status=offline`, and
the last known status remains available as `last_status`; an expired current task
is not exposed as active.

A heartbeat is accepted only after a successful handshake has registered the
configured connector. Presence is operational telemetry, not proof of a real
WorkBuddy Desktop UI session or task execution. The first implementation tracks
the single configured connector identity; multi-device registration and history
are out of scope.
