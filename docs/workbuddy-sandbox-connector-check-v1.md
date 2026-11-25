# WorkBuddy isolated sandbox connector check (T56)

## Purpose

The `connector-check` command verifies the local sandbox Hub's authenticated
WorkBuddy handshake, presence read, heartbeat receipt, and persisted presence.
It is a task-free smoke check for the sandbox API boundary.

It does **not** launch or connect WorkBuddy Desktop. A successful result is not
evidence that a real Desktop process, WorkBuddy Connector UI, or production
environment is connected.

## Run locally

From a disposable checkout of this development branch:

```powershell
python scripts/workbuddy_sandbox.py init
python scripts/workbuddy_sandbox.py up
python scripts/workbuddy_sandbox.py connector-check
python scripts/workbuddy_sandbox.py down
```

The script reads the generated `.env.workbuddy-sandbox` file. It uses only
`A2A_WORKBUDDY_CONNECTOR_TOKEN` for the connector API and the fixed sandbox
identity `workbuddy-desktop-sandbox`, which matches
`deploy/test/workbuddy-sandbox.compose.yaml`. It never prints the token.

## Exact request sequence

All requests use a direct HTTP connection to `127.0.0.1` and the configured
sandbox port. The destination host cannot be supplied by a caller.

1. `POST /integrations/workbuddy/v1/handshake` with protocol 1.0 and only the
   `heartbeat` capability.
2. `GET /integrations/workbuddy/v1/presence`; require the configured connector
   to be online after the handshake.
3. `POST /integrations/workbuddy/v1/heartbeat` with status `idle` and no task ID.
4. `GET /integrations/workbuddy/v1/presence`; require the matching heartbeat
   receipt ID to be durably visible and the connector to remain online/idle.

The command accepts only those three fixed connector endpoints. It does not
call `claim`, `start`, `renew`, `progress`, `result`, or any task endpoint;
it creates no task and has no task-execution side effect. Responses are size
limited and error messages do not echo credentials or response bodies.

## Interpreting PASS

A PASS means only that the loopback sandbox Hub accepted the dedicated
connector token, completed the protocol handshake, accepted one idle heartbeat,
and exposed the matching receipt through its presence endpoint.

The JSON result deliberately includes:

- `task_claimed: false`
- `desktop_process_verified: false`

It is an offline/local Hub API check, not a WorkBuddy Desktop acceptance test,
not proof of a live user session, and not a production connectivity test.

## Verification

```powershell
python -m pytest -q tests/test_workbuddy_sandbox.py
python -m compileall -q scripts tests
python -m pytest -q
```

Use only temporary local sandbox state. Do not paste, commit, or upload
`.env.workbuddy-sandbox`. To delete the sandbox volume, the existing explicit
confirmation gate remains required:

```powershell
python scripts/workbuddy_sandbox.py destroy --confirm-delete-data
```
