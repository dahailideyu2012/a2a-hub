# T10 — Live integration preflight

## Purpose

Before attempting the first controlled GitHub ↔ Hub ↔ WorkBuddy round trip, run an offline configuration gate. It prevents a live test from starting with missing credentials, shared credentials, non-durable task storage, overlapping database files, absent agent/repository allowlists, or a non-HTTPS public URL.

## Run

```bash
python scripts/live_integration_preflight.py
python scripts/live_integration_preflight.py --json
python -m pytest -q tests/test_live_integration_preflight.py
```

Exit code `0` means configuration is READY; exit code `1` means BLOCKED. The command makes no network requests and never prints credential values.

## Required boundaries

- Configure distinct credentials for general API auth, ChatGPT ingress, WorkBuddy connector, GitHub relay HMAC, and GitHub API callback.
- Use SQLite for Pull task persistence and three distinct database paths for tasks, ingress idempotency, and callback outbox.
- Configure at least one common allowed agent for ChatGPT ingress and WorkBuddy Pull.
- Configure exact `owner/repo` callback allowlist entries and an HTTPS public Hub URL.
- Use a dedicated fine-grained GitHub token restricted to Issues: write on the selected sandbox repository. The script cannot inspect token scopes.
- Use a disposable sandbox Issue. Never include secrets or personal data in task prompts or GitHub comments.

## What READY does not mean

READY is only a local configuration result. It does not test DNS/TLS, public reachability, GitHub permissions, GitHub writes, WorkBuddy desktop connectivity, or production readiness. It is not authorization to write to GitHub or deploy. The first live run must be separately approved and explicitly target a disposable test Issue.
