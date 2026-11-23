# T15-B — Disposable WorkBuddy Desktop sandbox

## Purpose and safety boundary

This stack creates a separate disposable Hub for a real WorkBuddy Desktop Pull round trip. It does not connect to the production Hub, GitHub Relay, Cloudflare, paid agents, or any public endpoint. The host port binds only to 127.0.0.1. The container has a dedicated SQLite volume and a one-agent registry. Its only agent is an echo-type synthetic target, so a Hub-mode task cannot invoke WorkBuddy CLI or a paid model.

This validates WorkBuddy Desktop ↔ local Hub only. It does not prove ChatGPT product-side connectivity. Do not repoint or overwrite an existing production Connector; create a separate sandbox Connector profile if the WorkBuddy UI supports it.

## Windows PowerShell

From the repository root:

```powershell
python scripts/workbuddy_sandbox.py init
python scripts/workbuddy_sandbox.py up
```

The init command creates deploy/test/.env.workbuddy-sandbox with three independent random credentials. The file is ignored by Git and created with restrictive permissions where supported. The script never prints credentials. Keep the file local; do not paste its contents into ChatGPT, GitHub, or task prompts.

The local Hub URL is http://127.0.0.1:18081. Configure a separate sandbox WorkBuddy Connector profile to use this Hub's WorkBuddy connector API and the dedicated A2A_WORKBUDDY_CONNECTOR_TOKEN from the local env file. The API base path is:

```
http://127.0.0.1:18081/integrations/workbuddy/v1
```

Use the sandbox token only in the Connector's local secret/configuration field. Do not change the existing connector profile. If WorkBuddy cannot register a second profile or its connector process cannot reach host loopback, stop and report BLOCKED; do not expose the Hub publicly as a workaround.

## Synthetic canary task

After the sandbox Connector profile is configured and trusted by the user:

```powershell
python scripts/workbuddy_sandbox.py seed
```

This creates one harmless mode=pull task for agent workbuddy and prints only its task ID and state. In WorkBuddy Desktop, explicitly ask it to claim and execute one Hub task using the a2a-workbuddy-pull Skill. The prompt asks only for the literal response WORKBUDDY_SANDBOX_OK and forbids file, network, credential, or external-service access.

After WorkBuddy reports completion:

```powershell
python scripts/workbuddy_sandbox.py status <TASK_ID>
```

Expected terminal state: completed. WorkBuddy's result must contain the exact canary phrase. Record the task ID, claim/start/progress/result outcomes, and Hub readback in the acceptance report. Do not include tokens or lease values.

## Stop and cleanup

```powershell
python scripts/workbuddy_sandbox.py down
```

This stops the container but preserves the isolated volume. To permanently remove the sandbox volume:

```powershell
python scripts/workbuddy_sandbox.py destroy --confirm-delete-data
```

The local env file is intentionally retained; delete it manually after the sandbox has been removed. The destroy command requires the explicit flag because it deletes sandbox task data.

## Guarantees

- init creates three distinct 48-byte URL-safe credentials and refuses to overwrite an existing env file.
- up builds and starts only the dedicated compose stack.
- seed enqueues one synthetic Pull task; it never prints secrets.
- status reads only the specified sandbox task.
- down preserves sandbox data; destroy requires explicit confirmation.
- No automatic polling, task claiming, WorkBuddy configuration edits, public tunnel, GitHub writes, deployment, or production access.

A running container is not a live WorkBuddy acceptance. Only real Desktop execution and Hub readback establish that result.
