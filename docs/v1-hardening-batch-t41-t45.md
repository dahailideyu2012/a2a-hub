# A2A Hub V1 hardening batch (T41–T45)

This batch extends the T34–T40 candidate on a stacked development branch.
It is not authorization to merge, migrate production, or deploy.

## T41 — Close readiness coverage gap

Add a regression test that disables every registered agent and proves
`/readyz` returns HTTP 503 with `checks.agents=none_enabled`, while
`/health` remains HTTP 200 as a liveness endpoint. This closes WorkBuddy's
P2-1 finding from the T34–T40 acceptance.

## T42 — Agent search filtering and query boundaries

Add automated HTTP and JSON-RPC coverage proving disabled and health-unavailable
agents are excluded from ranked search results, and invalid empty/oversized
queries are rejected at the HTTP boundary.

## T43 — Complete ingress route rate-limit coverage

Exercise all five ChatGPT ingress routes (submit, list, get, cancel, events)
against the same shared token budget. Each route must reject with HTTP 429
after the budget is consumed, before route-specific task lookup or mutation.

## T44 — Stable rate-limit identity during policy changes

The persisted bucket key is derived from a domain-separated token identity,
not from the configured numeric budget. Changing a budget must not silently
create a fresh bucket and reset the active window. Token rotation still yields
a distinct identity and budget.

## T45 — Security regression and operations documentation

Record the stable bucket semantics, route coverage, and readiness/search
contracts. Keep multi-host coordination limitations explicit: local SQLite
files on separate hosts are not a distributed rate limiter.

## Verification gates

- Full `pytest -q` and `python -m compileall -q a2a_hub tests run.py`.
- PostgreSQL 16 integration tests and wheel migration inclusion.
- Verify all five ingress routes share the same persisted budget.
- Verify changing the budget cannot reset an active window.
- Verify no raw token, DSN, or exception detail is persisted or returned.
- WorkBuddy performs one consolidated read-only acceptance after exact-HEAD
  CI is fully green. No merge or production deployment is authorized.
