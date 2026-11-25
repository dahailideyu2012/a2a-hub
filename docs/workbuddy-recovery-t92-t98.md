# WorkBuddy connection recovery and diagnostics (T92–T98)

## Scope

This batch adds deterministic, bounded recovery recommendations to the existing
authenticated diagnostics response. It is advisory only. It does not open network
connections, issue heartbeats, claim or execute tasks, or start a background retry worker.

## Recovery decision table

| Current state | Error category | Recommendation | Retry |
|---|---|---|---|
| online | any allowlisted/none | none | no |
| degraded | transient/none | heartbeat_probe | no |
| offline | authentication_failed / protocol_mismatch | operator_intervention | no |
| offline | retry budget exhausted | operator_intervention | no |
| offline | other/none | reconnect | bounded backoff |

Backoff is exponential, deterministic for diagnostics, and capped by max_seconds.
Defaults are 2 seconds, 300 seconds and 8 attempts. Invalid state, error category,
attempt count, time or policy values fail closed.

## Diagnostics contract

The authenticated diagnostics endpoint retains existing fields and adds:
- diagnostics_version: 2
- recovery.action
- recovery.retry_allowed
- recovery.retry_after_seconds
- recovery.retry_at
- recovery.attempts_remaining
- recovery.reason
- recovery.automatic_actions_enabled (always false in this release)

The no-handshake response also includes the same recovery envelope, with
connection_state=offline. Existing consumers can ignore the additive fields.

## Safety and rollout

The endpoint remains protected by the dedicated WorkBuddy connector token and
existing rate limit. No credential, raw exception, lease token, or task payload
is added to diagnostics. This is not proof of a live WorkBuddy Desktop session.
A future reconnect executor requires a separate opt-in, bounded network policy,
cancellation and audit tests.
