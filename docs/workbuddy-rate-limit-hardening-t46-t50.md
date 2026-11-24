# WorkBuddy connector rate-limit hardening (T46–T50)

This development-only batch hardens the WorkBuddy connector's shared SQLite
sliding-window budget. It does not authorize merge, production migration, or
deployment.

## T46 — Stable bucket identity across policy changes

The rate-limit bucket is derived from a domain separator, connector credential,
and configured connector identity. The numeric request budget is policy rather
than identity and is deliberately excluded from the key. Raising or lowering
the configured limit therefore changes enforcement immediately without
creating a fresh bucket or resetting an active window.

## T47 — Credential rotation isolation

A rotated connector token creates a distinct bucket. The previous token must
fail authentication and must not consume the new credential's budget. Raw
tokens remain absent from persisted bucket identifiers.

## T48 — Connector identity isolation

A changed configured connector identity creates a distinct budget namespace,
even when the credential is unchanged. The identity remains server-configured;
request bodies cannot select another connector's quota.

## T49 — Durable budget across router recreation

A newly constructed router with an empty in-process rate-store cache must still
observe the existing budget when configured with the same durable SQLite path.
This is a local persistence check, not proof of cross-host coordination.

## T50 — Fail-closed storage outage

If the rate-limit database is unavailable, connector requests return HTTP 503
before task-store access. Public responses must not disclose the database path
or connector credential.

## Verification

Run:

- pytest -q tests/test_workbuddy_connector_security.py
- pytest -q tests/test_workbuddy_connector.py tests/test_workbuddy_connector_security.py
- pytest -q
- python -m compileall -q a2a_hub tests run.py workbuddy_mcp_server.py

The suite must also retain the existing ten-endpoint shared-budget coverage.
Use temporary SQLite files only. Do not access production, real credentials,
real WorkBuddy Desktop, or real task execution during offline acceptance.
