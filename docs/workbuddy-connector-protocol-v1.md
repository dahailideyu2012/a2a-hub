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

Handshake and heartbeat receipts are stateless in this initial protocol increment:
they do not create a durable session, maintain a server-side online/offline registry,
or launch a WorkBuddy process. Real Desktop integration still requires a supported
client/connector to call these endpoints and a separately authorized sandbox
round-trip. The existing test suite uses mocked clients and isolated temporary
stores; it does not claim real Desktop acceptance.
