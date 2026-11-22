"""ChatGPT Remote MCP protocol and tool authorization tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

TOKEN = "test-chatgpt-ingress-token-0123456789"
MCP = "/integrations/chatgpt/mcp"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(client: TestClient, method: str, params=None, request_id=1, headers=None):
    return client.post(
        MCP,
        headers=headers or HEADERS,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def call_tool(client: TestClient, name: str, arguments: dict, request_id=2):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments}, request_id)
    assert response.status_code == 200
    return response.json()["result"]


class TestChatGPTRemoteMCP:
    def test_initialize_negotiates_supported_protocol(self, client):
        response = rpc(
            client,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        assert response.status_code == 200
        body = response.json()["result"]
        assert body["protocolVersion"] == "2025-06-18"
        assert body["capabilities"]["tools"]["listChanged"] is False
        assert body["serverInfo"]["name"] == "a2a-hub-chatgpt"

    def test_tools_list_exposes_only_restricted_task_tools(self, client):
        response = rpc(client, "tools/list")
        assert response.status_code == 200
        tools = response.json()["result"]["tools"]
        assert {item["name"] for item in tools} == {
            "submit_task", "get_task", "list_tasks", "get_task_events", "cancel_task"
        }
        assert all(item["inputSchema"]["additionalProperties"] is False for item in tools)

    def test_missing_and_invalid_bearer_are_rejected(self, client):
        missing = rpc(client, "tools/list", headers={"Content-Type": "application/json", "Accept": "application/json"})
        invalid = rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer invalid"})
        assert missing.status_code == 401
        assert invalid.status_code == 403

    def test_submit_then_get_and_list_task(self, client):
        submitted = call_tool(
            client,
            "submit_task",
            {"agent_id": "echo-a", "prompt": "mcp remote smoke test", "mode": "pull", "idempotency_key": "remote-mcp-test-0001"},
        )
        assert submitted["isError"] is False
        task = submitted["structuredContent"]["task"]
        task_id = task["id"]

        fetched = call_tool(client, "get_task", {"task_id": task_id})
        assert fetched["structuredContent"]["id"] == task_id

        listed = call_tool(client, "list_tasks", {"limit": 10})
        assert task_id in {item["id"] for item in listed["structuredContent"]["tasks"]}

    def test_event_tool_returns_owned_snapshot_checkpoint(self, client):
        submitted = call_tool(
            client,
            "submit_task",
            {"agent_id": "echo-a", "prompt": "event checkpoint", "mode": "pull", "idempotency_key": "remote-mcp-events-001"},
        )
        task_id = submitted["structuredContent"]["task"]["id"]
        result = call_tool(client, "get_task_events", {"task_id": task_id})
        checkpoint = result["structuredContent"]
        assert checkpoint["task_id"] == task_id
        assert checkpoint["events"][0]["type"] == "task_snapshot"
        assert checkpoint["stream_endpoint"].endswith(f"/tasks/{task_id}/events")

    def test_tool_rejects_unknown_fields(self, client):
        result = call_tool(client, "submit_task", {"agent_id": "echo-a", "prompt": "no extras", "admin": True})
        assert result["isError"] is True

    def test_non_post_methods_are_not_supported(self, client):
        assert client.get(MCP).status_code == 405
        assert client.delete(MCP).status_code == 405

    def test_unsupported_protocol_header_is_rejected(self, client):
        response = client.post(
            MCP,
            headers={**HEADERS, "MCP-Protocol-Version": "1900-01-01"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 400

    def test_mcp_body_limit_is_enforced(self, client):
        response = client.post(
            MCP,
            headers=HEADERS,
            content=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "submit_task", "arguments": {
                    "agent_id": "echo-a", "prompt": "x" * 20000
                }},
            }),
        )
        assert response.status_code == 413
