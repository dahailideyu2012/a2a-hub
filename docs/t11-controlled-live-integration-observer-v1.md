# T11 — Controlled live-integration observer

## Scope

T11 introduces a **read-only observer** for a manually initiated sandbox round trip. It does not trigger GitHub workflows, post comments, claim WorkBuddy tasks, or write to the Hub. A human/authorized operator posts the existing `/a2a submit <agent-id>` command on a disposable Issue; the existing relay and WorkBuddy connector perform their normal flow.

## Offline default

The default command is offline and performs no network requests:

```bash
python scripts/live_integration_observer.py --repo OWNER/SANDBOX --issue 123 --json
```

It returns `DRY_RUN`. No GitHub token is required.

## Explicit read-only observation

Set a dedicated GitHub token with **Issues: read-only** access to the sandbox repository only, and set the exact repository allowlist:

```powershell
$env:A2A_LIVE_TEST_REPOSITORY = "OWNER/SANDBOX"
$env:A2A_LIVE_GITHUB_READ_TOKEN = "<dedicated-read-only-token>"
python scripts/live_integration_observer.py --repo OWNER/SANDBOX --issue 123 --observe --confirm-read-only-github --json
```

The observer performs GitHub GET requests only. It verifies the exact repository allowlist, an open non-PR Issue, and the `a2a-relay-enabled` label. It scans at most 1,000 comments. Exactly 1,000 comments are accepted after an additional empty-page probe; any comment beyond that limit is rejected (fail-closed).

Accepted task IDs are normalized for result-marker matching by removing characters outside `A-Z a-z 0-9 _ -` and truncating to 100 characters, matching the callback marker generator. If normalization produces an empty key, observation is blocked rather than reporting a false match.

It reports one of:

- `WAITING_FOR_RELAY`: no Hub acceptance comment found.
- `WAITING_FOR_WORKBUDDY`: accepted task found, but no matching terminal result marker.
- `ROUNDTRIP_CONFIRMED`: the accepted task ID has a matching `a2a-hub-result` marker.

Exit codes: 0 = round trip confirmed (or offline dry-run); 2 = blocked/invalid setup; 3 = still waiting.

## Security boundaries

- No write-capable GitHub token is needed or accepted by design; use a dedicated read-only token.
- The observer never prints Issue bodies, task prompts, WorkBuddy output, tokens, or response headers.
- It never creates comments, dispatches workflows, or changes labels.
- The exact sandbox repository must match `A2A_LIVE_TEST_REPOSITORY`.
- The target must be an open Issue, not a pull request, and must carry `a2a-relay-enabled`.
- This is observation only. A matching result marker proves the callback comment exists; it does not independently attest to WorkBuddy desktop internals or production readiness.
- Actual task initiation remains a separate human action. Do not use production repositories or personal data.

## Tests

```bash
python -m pytest -q tests/test_live_integration_observer.py
python -m compileall -q scripts tests
```
