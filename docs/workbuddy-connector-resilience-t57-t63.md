# WorkBuddy connector resilience batch (T57–T63)

## Existing behavior

The Hub already persists server-observed presence in SQLite and derives online/offline
from heartbeat TTL. This batch does not duplicate that server-side store. The new
ConnectorRuntimeStore is a separate client-side state primitive for a future runtime.

## Delivered capabilities

- T57: SQLite-backed local runtime state survives process restart. It stores identity,
  negotiated protocol, client version, last heartbeat receipt/time, connection state,
  and reconnect attempt count. It has no credential or lease-token columns.
- T58: heartbeat age is classified as online, degraded, or offline. The authenticated
  diagnostics endpoint exposes the redacted liveness view.
- T59: bounded exponential reconnect backoff with optional jitter. This is a policy
  primitive, not an autonomous network reconnect loop.
- T60: explicit protocol negotiation. The server advertises supported versions and
  returns HTTP 426 for unsupported versions. Current support remains protocol 1.0.
- T61: runtime rows are keyed by connector ID; tests prove identity isolation.
- T62: authenticated GET /integrations/workbuddy/v1/diagnostics returns operational
  metadata without credentials or lease tokens. Diagnostics events use the existing
  structured redacted audit logger.
- T63: regression coverage for persistence/restart, TTL boundaries, backoff bounds,
  protocol rejection, identity isolation, authentication, and redaction.

## Operational boundary

These are reusable state/policy primitives. This change does not claim a running
WorkBuddy Desktop agent, an autonomous reconnect daemon, a real Desktop session,
or live task execution. No production credentials are stored or used.
