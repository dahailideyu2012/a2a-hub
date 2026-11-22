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
    request_headers = {**HEADERS, **(headers or {})}
    if method != "initialize":
        request_headers.setdefault("MCP-Protocol-Version", "2025-11-25")
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        payload["id"] = request_id
    return client.post(MCP, headers=request_headers, json=payload)


def call_tool(client: TestClient, name: str, arguments: dict, request_id=2):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments}, request_id)
    assert response.status_code == 200
    return response.json()["result"]


class TestChatGPTRemoteMCP:
    def test_initialize_negotiates_supported_protocol(self, client):
        response = rpc(
            client,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        assert response.status_code == 200
        body = response.json()["result"]
        assert body["protocolVersion"] == "2025-11-25"
        assert body["capabilities"]["tools"]["listChanged"] is False
        assert body["serverInfo"]["name"] == "a2a-hub-chatgpt"
        assert response.headers["MCP-Protocol-Version"] == "2025-11-25"

    def test_tools_list_exposes_only_restricted_task_tools(self, client):
        response = rpc(client, "tools/list")
        assert response.status_code == 200
        assert response.headers["MCP-Protocol-Version"] == "2025-11-25"
        tools = response.json()["result"]["tools"]
        assert {item["name"] for item in tools} == {
            "submit_task", "get_task", "list_tasks", "get_task_events", "cancel_task"
        }
        assert all(item["inputSchema"]["additionalProperties"] is False for item in tools)

    def test_missing_and_invalid_bearer_are_rejected(self, client):
        missing = rpc(client, "tools/list", headers={"Content-Type": "application/json", "Accept": "application/json"})
        invalid = rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer test-workbuddy-connector-token-0123456789"})
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


    def test_initialized_notification_requires_and_returns_protocol_header(self, client):
        response = rpc(client, "notifications/initialized", request_id=None)
        assert response.status_code == 202
        assert response.headers["MCP-Protocol-Version"] == "2025-11-25"

    def test_post_initialize_request_requires_protocol_header(self, client):
        response = client.post(
            MCP,
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 400
        assert "MCP-Protocol-Version" in response.json()["detail"]

    def test_initialize_requires_request_id(self, client):
        response = rpc(client, "initialize", request_id=None)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32600

    def test_accept_must_advertise_both_streamable_http_response_types(self, client):
        response = client.post(
            MCP,
            headers={**HEADERS, "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert response.status_code == 406

    def test_mcp_tool_call_response_includes_protocol_header(self, client):
        response = rpc(
            client,
            "tools/call",
            {"name": "submit_task", "arguments": {
                "agent_id": "echo-a",
                "prompt": "protocol response header",
                "idempotency_key": "mcp-proto-header-001",
            }},
        )
        assert response.status_code == 200
        assert response.headers["MCP-Protocol-Version"] == "2025-11-25"
        assert response.json()["result"]["isError"] is False


    def test_chatgpt_token_is_not_interchangeable_with_workbuddy_token(self, client):
        response = rpc(
            client,
            "tools/list",
            headers={**HEADERS, "Authorization": "Bearer test-workbuddy-connector-token-0123456789"},
        )
        assert response.status_code == 403

    def test_short_chatgpt_token_fails_closed(self, client):
        from a2a_hub.server import hub

        original = hub.settings.chatgpt_ingress_token
        try:
            hub.settings.chatgpt_ingress_token = "short"
            response = rpc(client, "tools/list")
            assert response.status_code == 503
        finally:
            hub.settings.chatgpt_ingress_token = original

    def test_agent_allowlist_is_enforced_through_mcp(self, client):
        result = call_tool(
            client,
            "submit_task",
            {"agent_id": "echo-b", "prompt": "not allowlisted", "idempotency_key": "mcp-denied-agent-001"},
        )
        assert result["isError"] is True

    def test_task_ownership_is_enforced_through_mcp(self, client):
        result = call_tool(client, "get_task", {"task_id": "task-not-owned-by-chatgpt"})
        assert result["isError"] is True

    def test_idempotent_submit_replays_same_task(self, client):
        args = {
            "agent_id": "echo-a",
            "prompt": "same logical MCP task",
            "mode": "pull",
            "idempotency_key": "mcp-idempotency-replay-001",
        }
        first = call_tool(client, "submit_task", args)
        second = call_tool(client, "submit_task", args)
        assert first["isError"] is False
        assert second["isError"] is False
        assert first["structuredContent"]["task"]["id"] == second["structuredContent"]["task"]["id"]

    def test_cancel_pull_task_through_mcp(self, client):
        submitted = call_tool(
            client,
            "submit_task",
            {"agent_id": "echo-a", "prompt": "cancel me", "mode": "pull", "idempotency_key": "mcp-cancel-task-001"},
        )
        task_id = submitted["structuredContent"]["task"]["id"]
        cancelled = call_tool(client, "cancel_task", {"task_id": task_id})
        assert cancelled["isError"] is False
        assert cancelled["structuredContent"]["status"]["state"].lower() == "canceled"

    def test_unknown_jsonrpc_method_returns_method_not_found(self, client):
        response = rpc(client, "not/a/real/method")
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32601

    def test_invalid_json_returns_parse_error(self, client):
        response = client.post(
            MCP,
            headers=HEADERS,
            content=b"{not-json",
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32700


    def test_jsonrpc_null_id_is_rejected_not_treated_as_notification(self, client):
        response = client.post(
            MCP,
            headers={**HEADERS, "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": None, "method": "tools/list"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32600

    def test_invalid_content_type_is_rejected(self, client):
        response = client.post(
            MCP,
            headers={**HEADERS, "Content-Type": "text/plain"},
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
        )
        assert response.status_code == 415
