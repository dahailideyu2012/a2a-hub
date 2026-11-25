# WorkBuddy heartbeat sequence guard (T78–T84)

## Goal

Prevent delayed, duplicated, or reordered sequenced heartbeat requests from
overwriting newer server-observed connector presence. This is an additive
protocol extension; legacy clients that omit `heartbeat_seq` continue to work.

## Contract

- T78: the presence SQLite store adds nullable `last_heartbeat_seq` through
  an additive migration. Existing rows and presence fields are preserved.
- T79: clients may send a positive integer `heartbeat_seq` in heartbeat
  requests. Booleans, floats, strings, zero, and negative values are invalid.
- T80: when a sequence is supplied, the server atomically accepts it only if
  it is strictly greater than the persisted sequence. Duplicate and stale
  values receive HTTP 409 and do not update presence.
- T81: a successful handshake starts a new connection epoch and resets the
  stored heartbeat sequence to null.
- T82: heartbeat receipts echo the accepted sequence (or null for legacy
  unsequenced clients).
- T83: authenticated presence/diagnostics expose the last accepted sequence.
- T84: regression tests cover migration, ordering, replay, handshake reset,
  malformed values, API behavior, and legacy compatibility.

## Compatibility

The sequence field is optional. If omitted, the existing heartbeat behavior
is preserved and the stored sequence is not changed. A client should use
monotonically increasing sequence values within one handshake epoch and reset
its counter after reconnecting and completing a new handshake.

## Safety and non-goals

This is a server-side freshness guard, not proof of a real Desktop session.
It does not enable autonomous polling, task claiming, task execution, new
privileges, or production access. Credentials are not stored or returned.
