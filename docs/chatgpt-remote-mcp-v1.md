# ChatGPT Remote MCP 接入（V1）

## Endpoint

- MCP Streamable HTTP: `POST /integrations/chatgpt/mcp`
- REST fallback: `/integrations/chatgpt/v1`
- MCP protocol versions negotiated: `2025-03-26`, `2025-06-18`
- Transport is stateless: no MCP session is created or persisted.

The MCP endpoint exposes only five task tools:

| Tool | Purpose |
| --- | --- |
| `submit_task` | Submit a task to an allowlisted agent; defaults to Pull mode |
| `get_task` | Read a task owned by the ChatGPT ingress |
| `list_tasks` | List tasks submitted through this ingress |
| `get_task_events` | Return the current persisted task snapshot as an event checkpoint |
| `cancel_task` | Cancel an owned Pull task when the configured repository supports safe cancellation |

`get_task_events` currently returns a persisted snapshot checkpoint, not a replay of the complete event history. Live events remain available from the REST SSE route `GET /integrations/chatgpt/v1/tasks/{task_id}/events`. Do not describe this snapshot tool as historical event streaming.

## Authentication and configuration

Configure the existing dedicated ChatGPT ingress token. The token must be at least 32 characters and must be provided as:

```http
Authorization: Bearer <A2A_CHATGPT_INGRESS_TOKEN>
```

The MCP endpoint delegates tool execution to the same restricted REST ingress router. Therefore it shares:
- dedicated high-entropy token validation and constant-time comparison;
- per-process rate limiting;
- explicit agent allowlist;
- strict request schemas and request body limit;
- idempotency ledger and task ownership checks;
- task persistence and cancellation restrictions.

Do not reuse the WorkBuddy connector token. Do not put tokens in URLs, source control, prompts, task output, or logs. Use HTTPS for any remote deployment. HTTP is only suitable for local loopback testing.

## Local protocol smoke test

Send an MCP `initialize` request:

```http
POST /integrations/chatgpt/mcp
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "mcp-inspector", "version": "1"}
  }
}
```

Then send `notifications/initialized`, followed by `tools/list` and `tools/call` requests. The server returns JSON-RPC responses using the Streamable HTTP transport. GET and DELETE are intentionally not supported in this stateless implementation.

## ChatGPT product compatibility

A working Remote MCP endpoint does not by itself grant every ChatGPT plan access to custom MCP tools. Check current workspace eligibility and permissions before planning a product-side connection. OpenAI currently documents full custom MCP connections for Business and Enterprise/Edu on ChatGPT web; Pro custom connections are limited to read/fetch permissions. Free accounts are not listed as eligible for full custom MCP connections.

References:
- [OpenAI: Add a custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OpenAI Help: Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

## Release boundary

This implementation is code-only on `feature/chatgpt-workbuddy-bridge-v1`. It has not been deployed, exposed publicly, connected to production credentials, or merged into `main`. Before release, run the dedicated tests, full test suite, static checks, MCP Inspector, and a real ChatGPT workspace connection test. A real ChatGPT account test is blocked until an eligible workspace and an authorized user are available.
