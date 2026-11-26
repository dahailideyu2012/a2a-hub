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


## MCP bridge handshake and presence client (T18)

The stdio bridge in `workbuddy_mcp_server.py` now performs the authenticated
protocol handshake automatically before its first task claim. It sends the
configured connector identity (`A2A_WORKBUDDY_CONNECTOR_ID`, default
`workbuddy-personal`), protocol version 1.0, client version, and supported
capabilities. The bridge fails closed if the Hub does not accept the exact
configured identity. The successful handshake is cached for that MCP process;
restart the process to renegotiate after changing connector configuration.

Two explicit MCP tools are available:

- `workbuddy_connector_status`: handshake if needed, then issue an authenticated
  GET to `/presence`. The returned online flag is TTL-based telemetry only.
- `workbuddy_heartbeat`: handshake if needed, then send one authenticated
  heartbeat with status `idle`, `busy`, or `degraded`. This is a single
  request, not a background polling loop.

`workbuddy_claim_task` also ensures handshake before attempting a claim. The
bridge still holds lease tokens only in its process-local private map, limits
itself to one active lease, and exposes no arbitrary shell/command execution
tool. Progress and result retries remain restricted to server-defined
idempotent operations. The MCP bridge does not autonomously execute tasks:
WorkBuddy must invoke the task tools and decide how to perform authorized work.


## Cross-transport acceptance path (T19)

The end-to-end task loop joins the separate ChatGPT and WorkBuddy credentials without sharing them:

- ChatGPT submits a Pull task through the restricted ChatGPT Remote MCP `submit_task` tool.
- WorkBuddy independently authenticates with the WorkBuddy connector token, claims one allowlisted task, and holds the lease token only in its local bridge process.
- WorkBuddy explicitly starts the task, reports ordered progress, and submits a terminal result.
- ChatGPT reads the same task through its ingress-owned `get_task` / `get_task_events` tools.

The automated contract test `tests/test_chatgpt_workbuddy_loop.py` runs both MCP surfaces against one isolated Hub and temporary SQLite task/presence stores. It proves the software/API contract only; it does not launch WorkBuddy Desktop, simulate a user approval, or establish a production connection. No Hub-to-Desktop push is implied: WorkBuddy must pull and invoke each lifecycle tool.


## Connector request rate limit (T31)

The Hub applies a sliding-window request budget to every authenticated WorkBuddy
connector endpoint (claim/start/renew/progress/result, callback status/retry,
handshake/heartbeat/presence). Configure `A2A_WORKBUDDY_CONNECTOR_RATE_LIMIT`
(default `60`, allowed `1`–`600`) as requests per 60 seconds for the configured
connector identity. Authentication is checked before consuming the budget; a
missing or invalid token therefore does not consume an authenticated request slot.
Requests over budget return HTTP `429` before opening task, presence, or callback
storage for that endpoint.

The current limiter is in-process memory, reuses the Hub's sliding-window
implementation, and is intentionally **not shared between worker processes**.
With `N` Uvicorn workers the effective aggregate allowance may reach `N × limit`.
Use a single worker for a strict aggregate budget; shared atomic rate limiting is
reserved for T33. The current single configured connector identity means this is
one connector bucket, not a multi-device/token registry.


## Structured security audit (T31)

The connector emits one-line JSON audit records to the dedicated Python logger
`a2a_hub.audit.workbuddy` at INFO level. Events cover all ten connector routes:
claim, start, renew, progress, result, handshake, heartbeat, presence, callback
status, and callback retry. HTTP failures are recorded with their status and a
fixed reason code; the original exception text and response body are not logged.

The record uses a closed field set: event, outcome, configured connector ID,
HTTP status, an eight-hex-character SHA-256 token fingerprint (or `-` when no
token was supplied), duration in milliseconds, and an optional fixed reason
code. The raw token, lease token, task prompt, progress message, result output,
request/response bodies, and arbitrary exception text are never audit fields.
The logger uses its own namespace and does not configure or attach handlers to
the root logger. Audit records are telemetry, not task events, and are never
published to the Hub event bus.


## Cancellation polling contract (T32)

A successful authenticated `POST /heartbeat` now includes `cancel_requested`
(boolean) and `cancel_reason` (currently `task_cancelled_by_caller` or null).
When the connector reports its `current_task_id`, the Hub checks the durable
task row and returns a cancellation signal only when all three conditions hold:
the task is in terminal `canceled` state, it is a pull-mode task, and its
persisted connector assignment matches the server-configured connector identity.
A missing task, a task assigned to another connector, or a non-canceled task
returns false. The request body cannot choose the connector identity.

WorkBuddy should inspect this field on each heartbeat while executing a task,
stop local execution cooperatively when it becomes true, and submit no further
progress/result for that canceled task. This is a polling signal, not an
out-of-band interrupt: detection latency is bounded by the heartbeat interval
(30 seconds by default). Lease fencing remains the final server-side protection.
The endpoint remains backward-compatible because the response only adds fields.


## Cooperative cancellation checkpoints (T32-B)

The stdio MCP bridge exposes `workbuddy_check_task_cancellation(task_id)` for a
task already leased by the current MCP process. It sends one authenticated
`busy` heartbeat carrying that task ID and validates the Hub response identity,
task ID, acceptance flag, and boolean `cancel_requested` field.

- If `cancel_requested=false`, the local lease remains available and execution
  may continue.
- If `cancel_requested=true`, the bridge returns `should_stop=true` and
  immediately discards its local lease. Subsequent progress/result tool calls
  from that MCP process are blocked because the private lease is no longer held.
- If the Hub response is missing or malformed, the check fails closed and does
  not discard the lease; the caller must not interpret the error as a negative
  cancellation signal.

WorkBuddy should call this tool between meaningful steps of a long-running task
and immediately stop its own work when `should_stop=true`. This is cooperative
checkpointing only: it does not interrupt a running shell/process, does not
create a background polling loop, and does not replace server-side lease fencing.
The existing `workbuddy_heartbeat` remains available for ordinary liveness
updates; the dedicated tool additionally requires a locally held task lease and
provides a clear stop decision to the model.



## Operator-in-the-loop recovery decisions (T106–T112)

The authenticated `GET /integrations/workbuddy/v1/diagnostics` response is
version 4 and adds:

- `recovery_snapshot_id`: a stable 64-character SHA-256 identifier for the
  current redacted recovery snapshot;
- `operator_decision`: the most recent decision for that exact snapshot, or
  null when no current decision exists.

The MCP bridge exposes two task-free tools:

- `workbuddy_recovery_diagnostics` reads diagnostics without performing a
  handshake. This is deliberate: a successful handshake clears previous error
  and retry history, so a diagnostics read must not silently reset the state
  being inspected.
- `workbuddy_record_recovery_decision` records one of `acknowledged`,
  `deferred`, or `dismissed` for a supplied snapshot ID. Invoke it only after
  the user/operator has explicitly chosen a disposition.

The REST request is `POST /integrations/workbuddy/v1/recovery/decision`:

```json
{
  "connector_id": "workbuddy-personal",
  "snapshot_id": "<64-character recovery_snapshot_id>",
  "disposition": "deferred"
}
```

The endpoint uses the dedicated connector token, verifies the configured
connector identity, rejects unknown fields, and applies the existing connector
rate limit. The Hub uses a monotonic recovery revision and an atomic SQLite
compare-and-set. If the connection state changes after diagnostics was read,
the request returns HTTP 409 and the caller must fetch a fresh snapshot. An
exact retry of the same disposition is idempotent; a different disposition for
the same snapshot conflicts.

A decision receipt is an audit record only. `acknowledged` does not mean a
reconnect or heartbeat probe was executed, and `deferred`/`dismissed` do not
change the recovery policy. The response always sets
`action_executed=false` and `automatic_actions_enabled=false`. No automatic
network action, task claim, task execution, or background worker is added.
Diagnostics and decision tools do not expose lease tokens, connector tokens,
raw exception text, task prompts, or result payloads.
