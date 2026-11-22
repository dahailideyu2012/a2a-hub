# T9 — Outbox fault-injection and concurrency acceptance

## Scope

T9 adds deterministic, local-only regression coverage for the durable GitHub callback outbox. It does not change the delivery protocol or claim exactly-once network delivery.

## Failure windows covered

1. **Crash after claim, before network send:** a fresh process reopens the same SQLite file; the expired lease becomes claimable; the new lease receives a different fencing token and retains the callback payload.
2. **Remote accepted, response/ack lost:** the test models a remote endpoint with the same stable task marker semantics used by the GitHub callback adapter. The restarted worker retries the same task; two POST attempts produce one logical remote comment, then the current lease is acknowledged.
3. **Expired lease before reclaim:** the old worker cannot acknowledge after its lease expires, even before another worker claims the row.
4. **Parallel batch claims:** 16 concurrent workers claim 32 due tasks. Every task has exactly one owner in this claim batch, task IDs and fencing tokens are unique, and each winning token can acknowledge only its own row.

## Delivery guarantee

The outbox provides durable **at-least-once** delivery. A process can die after the remote service accepts a request but before SQLite records `delivered`; the request can therefore be retried. Duplicate suppression depends on the stable task marker and remote read-before-write behavior, not on SQLite alone. This is not a distributed exactly-once guarantee.

## Validation

```bash
python -m pytest -q tests/test_github_callback_outbox.py
python -m pytest -q tests/test_github_callback_outbox_faults.py
python -m pytest -q tests/test_workbuddy_connector.py
python -m pytest -q tests/test_github_callback_roundtrip.py
python -m pytest -q
python -m compileall -q a2a_hub scripts tests
```

All tests use temporary SQLite databases and synthetic callback metadata. No GitHub credentials, real GitHub writes, production Hub, Cloudflare, or WorkBuddy runtime are used.
