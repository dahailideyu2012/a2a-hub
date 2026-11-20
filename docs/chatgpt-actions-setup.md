# ChatGPT → A2A Hub REST Action

This document describes an optional ChatGPT REST Action contract for the existing restricted ChatGPT ingress. It is **not** a Remote MCP server and does not by itself make a local Hub reachable from ChatGPT cloud.

## Files

- OpenAPI schema: [chatgpt-actions-openapi.json](./chatgpt-actions-openapi.json)
- API implementation: `a2a_hub/chatgpt_ingress.py`
- API tests: `tests/test_chatgpt_ingress.py`

## What it supports

| Operation | Endpoint | Purpose |
|---|---|---|
| `submitA2AHubTask` | `POST /integrations/chatgpt/v1/tasks` | Create a task; use `mode=pull` for WorkBuddy |
| `listA2AHubTasks` | `GET /integrations/chatgpt/v1/tasks` | List ingress-owned tasks |
| `getA2AHubTask` | `GET /integrations/chatgpt/v1/tasks/{task_id}` | Read task state and result |
| `cancelA2AHubPullTask` | `POST /integrations/chatgpt/v1/tasks/{task_id}/cancel` | Cancel an eligible Pull task |

The SSE endpoint `GET /tasks/{task_id}/events` is intentionally omitted. The Action workflow reads task snapshots using `getA2AHubTask`; it does not provide a streaming UI.

## Required Hub configuration

Configure these values in the Hub's server-side secret/environment configuration. Never commit real values:

- `A2A_CHATGPT_INGRESS_TOKEN`: a high-entropy secret with at least 32 characters.
- `A2A_CHATGPT_INGRESS_ALLOWED_AGENTS`: explicit comma-separated agent IDs ChatGPT may submit to.
- `A2A_CHATGPT_INGRESS_ACTOR`: audit identity for ingress-created work.
- `A2A_CHATGPT_INGRESS_DB_PATH`: persistent ingress ledger path.
- `A2A_WORKBUDDY_CONNECTOR_TOKEN`: separate secret used by the WorkBuddy connector; do not reuse the ChatGPT ingress token.
- `A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS`: explicit agent IDs WorkBuddy may claim.

The ChatGPT and WorkBuddy tokens are separate credentials with separate purposes. The agent must be allowed by both relevant ingress/connector allowlists. Use a persistent SQLite task store for Pull mode; cancellation currently requires the SQLite task store.

## ChatGPT Action editor setup (optional compatibility path)

1. First deploy the Hub behind a public, valid HTTPS origin with authentication, rate limiting, monitoring, and a persistent database. Do not expose a development server or a home-PC loopback port.
2. Replace the placeholder `https://YOUR-HUB-DOMAIN` in the OpenAPI `servers[0].url` with that exact HTTPS origin.
3. Import `docs/chatgpt-actions-openapi.json` into an eligible GPT Action editor.
4. Set Authentication to **API Key → Custom Header**:
   - Header name: `X-ChatGPT-Ingress-Token`
   - Value: the Hub's `A2A_CHATGPT_INGRESS_TOKEN`
5. Keep the secret in the editor's authentication settings only. Never paste it into the schema, GPT instructions, prompt, or GitHub.
6. In the GPT instructions, tell the model to:
   - use the exact configured WorkBuddy agent ID;
   - submit with `mode: "pull"`;
   - create a fresh `Idempotency-Key` for each new task and reuse it only for an identical retry;
   - call `getA2AHubTask` to verify the final state and report only what the Hub returned;
   - never claim a task completed before the returned state is `completed`;
   - avoid sending secrets or unnecessary personal data in task prompts.
7. Test first with a harmless synthetic task and verify the resulting task ID/state in the Hub. Do not switch to production work until security and deployment review is complete.

OpenAI documents GPT Actions as REST API integrations configured with an OpenAPI schema and API-key authentication, including custom headers: https://help.openai.com/en/articles/9442513-configuring-actions-in-gpts

**Lifecycle caveat (checked 2026-11-20):** OpenAI's current Help Center article says Custom GPTs are planned to retire, with the standard retirement date scheduled for 2026-12-11. Treat this Action schema as a short-term compatibility/integration contract, not the long-term ChatGPT integration architecture. Re-check current OpenAI migration guidance before investing in a Custom GPT deployment.

## Important network limitation

The WorkBuddy configuration currently examined in this project points to `http://127.0.0.1:8080`. That address is reachable only from the same machine. A ChatGPT cloud Action cannot call a service bound only to your computer's loopback interface.

A real shared deployment requires both clients to reach the **same secured Hub origin**:
- ChatGPT Action: public HTTPS origin.
- WorkBuddy connector: the same Hub origin, with its separate WorkBuddy token.

Do not solve this by opening `0.0.0.0`, exposing port 8080, or creating an unreviewed tunnel. Deployment and any change to the user's WorkBuddy URL/configuration require a separate explicit approval and security review.

## Current acceptance status

- Isolated local ChatGPT REST ingress ↔ real A2A Hub ↔ WorkBuddy desktop AI: PASS (C2-B-07-01).
- Public HTTPS deployment: NOT DONE.
- ChatGPT cloud Action configured and tested against a deployed Hub: NOT DONE.
- WorkBuddy connected to a shared public Hub: NOT DONE.
- Remote MCP: NOT IMPLEMENTED by this REST Action.
