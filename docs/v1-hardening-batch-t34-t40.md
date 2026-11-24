# A2A Hub V1 hardening batch (T34–T40)

This batch is built on the T33-B PostgreSQL TaskStore candidate. It is a development
branch only; it does not authorize a merge, production migration, or deployment.

## T34 — Shared ChatGPT ingress rate limiting

- ChatGPT ingress now uses the existing atomic `SQLiteRateLimitStore` rather than a
  process-local deque.
- Every route under `/integrations/chatgpt/v1` consumes the same token-scoped
  60-second budget.
- The bucket identifier is a domain-separated SHA-256 digest; raw ingress tokens
  are never written to the rate-limit database.
- SQLite uses `BEGIN IMMEDIATE`, a persisted clock high-water mark, and fails
  closed with HTTP 503 if the limiter store is unavailable.
- Configure `A2A_CHATGPT_INGRESS_RATE_LIMIT_DB_PATH`. All workers on one host
  must point to the same durable file. Local SQLite files on different hosts do
  not provide a global distributed budget.

## T35 — Safe transport error boundary

Unexpected JSON-RPC implementation/driver exceptions are logged server-side but
return the stable public message `Internal server error`. Domain errors and
explicit unsupported-operation errors retain their intentional public messages.
This prevents DSNs, passwords, filesystem paths, and provider exception text
from being reflected to remote callers.

## T36 — Ranked agent discovery

- Adds authenticated HTTP `GET /agents/search?q=...&limit=1..20`.
- Adds JSON-RPC `agents/search` with the same query and limit contract.
- Results reuse the registry's skill/tag/description ranking and include score
  plus the existing AgentRecord snapshot.
- Disabled and unavailable agents are not advertised as routable search results.
- Agent cards and the existing static capability manifest remain available.

## T37 — Execution timeout input validation

- `A2A_TASK_TIMEOUT` is constrained to 1–86400 seconds at configuration load.
- Per-call timeout overrides must be finite and positive; zero, negative,
  infinity, and NaN are rejected rather than silently falling back or triggering
  an immediate timeout.

## T38 — Readiness and liveness separation

- `GET /health` remains a liveness response.
- `GET /readyz` checks task-store read availability and that at least one agent
  is enabled.
- Dependency failures return 503 with a bounded status payload; internal
  exception text is not exposed.

## T39 — Contract regression coverage

The CI suite covers shared budgets across independently constructed ingress
routers, existing multi-process SQLite atomicity, HTTP/JSON-RPC search parity,
invalid timeout inputs, JSON-RPC error redaction, and readiness failure behavior.

## T40 — Operations and deployment boundaries

- Keep ChatGPT ingress and WorkBuddy connector credentials independent.
- Use HTTPS at the deployment edge; never put tokens in URLs or logs.
- For multiple workers on one host, mount the same durable SQLite files for
  WorkBuddy and ChatGPT ingress rate limits and for the selected task backend.
- For multiple hosts, use a shared transactional database/backend; SQLite files
  on separate hosts are not a distributed coordination mechanism.
- Use `/health` for liveness and `/readyz` for readiness.
- Do not treat GitHub CI or repository tests as proof of real WorkBuddy Desktop
  connectivity. Real Desktop/Connector acceptance requires a separately
  authorized user session and an explicit end-to-end test window.

## Verification required before acceptance

1. Run full `pytest -q` and `python -m compileall -q a2a_hub tests run.py`.
2. Run PostgreSQL 16 integration tests with `A2A_TEST_POSTGRES_DSN` configured.
3. Repeat the spawn-based single-task PostgreSQL claim race test at least three
   times and verify that removing `FOR UPDATE SKIP LOCKED` makes it fail.
4. Run the SQLite rate-limit multi-process test and the new independent-router
   ChatGPT ingress test.
5. Review the complete PR diff and report P0/P1/P2. No merge or deployment is
   part of this batch.
