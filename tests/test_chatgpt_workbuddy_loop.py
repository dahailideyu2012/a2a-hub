"""Cross-transport ChatGPT -> Hub -> WorkBuddy -> Hub -> ChatGPT acceptance."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

CHATGPT_TOKEN = "test-chatgpt-ingress-token-0123456789"
WORKBUDDY_TOKEN = "test-workbuddy-connector-token-0123456789"
MCP_PATH = "/integrations/chatgpt/mcp"


@pytest.fixture
def loop_client(settings, tmp_path):
    """Use a durable isolated task store for the complete Pull lifecycle."""
    import a2a_hub.config as config
    import a2a_hub.registry as registry
    import a2a_hub.server as server
    from a2a_hub.ports import PullTaskRepository

    previous_hub = server.hub
    previous_settings = config._settings
    previous_registry = registry._registry
    previous_store, previous_path = settings.store, settings.db_path
    settings.store = "sqlite"
    settings.db_path = str(tmp_path / "chatgpt-workbuddy-loop.sqlite3")
    settings.workbuddy_presence_db_path = str(tmp_path / "workbuddy-presence.sqlite3")
    config._settings = settings
    registry._registry = None
    server.hub = server.Hub()
    assert isinstance(server.hub.registry.store, PullTaskRepository)
    try:
        with TestClient(server.app) as client:
            yield client
    finally:
        server.hub = previous_hub
        config._settings = previous_settings
        registry._registry = previous_registry
        settings.store, settings.db_path = previous_store, previous_path


def chatgpt_rpc(client: TestClient, method: str, params=None, request_id=1):
    headers = {
        "Authorization": f"Bearer {CHATGPT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if method != "initialize":
        headers["MCP-Protocol-Version"] = "2025-11-25"
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        body["id"] = request_id
    response = client.post(MCP_PATH, headers=headers, json=body)
    return response


def chatgpt_tool(client: TestClient, name: str, arguments: dict, request_id: int):
    response = chatgpt_rpc(
        client, "tools/call",
        {"name": name, "arguments": arguments}, request_id=request_id,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result.get("content")
    return result["structuredContent"]


def bridge_payload(result: dict) -> dict:
    assert result["isError"] is False, result.get("content")
    return json.loads(result["content"][0]["text"])


def test_chatgpt_submit_workbuddy_execute_and_chatgpt_readback(
    loop_client, monkeypatch
):
    """Exercise both MCP surfaces against one isolated Hub and SQLite task store."""
    import workbuddy_mcp_server as bridge

    # The WorkBuddy stdio bridge's authenticated HTTP transport is routed into
    # the same in-process FastAPI app. No external network or production data.
    def post(path: str, payload: dict) -> dict:
        response = loop_client.post(
            f"/integrations/workbuddy/v1/{path}",
            headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN},
            json=payload,
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", "Hub request failed")
            raise RuntimeError(f"Hub HTTP {response.status_code}: {detail}")
        return response.json()

    def get(path: str) -> dict:
        response = loop_client.get(
            f"/integrations/workbuddy/v1/{path}",
            headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN},
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", "Hub request failed")
            raise RuntimeError(f"Hub HTTP {response.status_code}: {detail}")
        return response.json()

    monkeypatch.setattr(bridge, "_post", post)
    monkeypatch.setattr(bridge, "_get", get)
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", WORKBUDDY_TOKEN)
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "workbuddy-personal")
    bridge.HANDSHAKE_READY = False
    with bridge.LEASES_LOCK:
        bridge.LEASES.clear()

    initialized = chatgpt_rpc(
        loop_client, "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "loop-test", "version": "1"}},
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
    assert chatgpt_rpc(loop_client, "notifications/initialized", request_id=None).status_code == 202

    tools = chatgpt_rpc(loop_client, "tools/list").json()["result"]["tools"]
    assert {"submit_task", "get_task", "get_task_events", "cancel_task"} <= {
        item["name"] for item in tools
    }

    submitted = chatgpt_tool(
        loop_client, "submit_task",
        {
            "agent_id": "echo-a",
            "prompt": "请完成一次端到端闭环测试，并返回唯一标记 LOOP-T19-OK",
            "mode": "pull",
            "idempotency_key": "chatgpt-workbuddy-loop-0001",
        },
        10,
    )
    task_id = submitted["task"]["id"]
    assert submitted["idempotent_replay"] is False
    assert submitted["task"]["status"]["state"].lower() == "submitted"

    # Connector handshake + authenticated presence + heartbeat.
    presence = bridge_payload(bridge.call_tool("workbuddy_connector_status", {}))
    assert presence["connector_id"] == "workbuddy-personal"
    heartbeat = bridge_payload(
        bridge.call_tool("workbuddy_heartbeat", {"status": "busy", "current_task_id": task_id})
    )
    assert heartbeat["receipt_id"]

    claimed = bridge_payload(bridge.call_tool("workbuddy_claim_task", {}))
    assert claimed["claimed"] is True
    assert claimed["task_id"] == task_id
    assert "LOOP-T19-OK" in claimed["prompt"]

    started = bridge_payload(bridge.call_tool("workbuddy_start_task", {"task_id": task_id}))
    assert started["status"]["state"].lower() == "working"
    progress = bridge_payload(
        bridge.call_tool(
            "workbuddy_progress_task",
            {"task_id": task_id, "sequence": 1, "message": "T19：WorkBuddy 已领取并开始执行"},
        )
    )
    assert progress["idempotent_replay"] is False

    final_output = "LOOP-T19-OK：ChatGPT 提交、WorkBuddy 领取/执行/回报、Hub 持久化、ChatGPT 回读全部成功。"
    finished = bridge_payload(
        bridge.call_tool(
            "workbuddy_submit_result",
            {"task_id": task_id, "state": "completed", "output": final_output},
        )
    )
    assert finished["idempotent_replay"] is False
    assert task_id not in bridge.LEASES

    fetched = chatgpt_tool(loop_client, "get_task", {"task_id": task_id}, 11)
    assert fetched["id"] == task_id
    assert fetched["status"]["state"].lower() == "completed"
    assert final_output in json.dumps(fetched, ensure_ascii=False)

    checkpoint = chatgpt_tool(
        loop_client, "get_task_events", {"task_id": task_id}, 12
    )
    assert checkpoint["task_id"] == task_id
    assert checkpoint["events"][0]["type"] == "task_snapshot"
    assert checkpoint["events"][0]["data"]["status"]["state"].lower() == "completed"

    # Re-submitting the same logical request returns the original task, never a duplicate.
    replay = chatgpt_tool(
        loop_client, "submit_task",
        {
            "agent_id": "echo-a",
            "prompt": "请完成一次端到端闭环测试，并返回唯一标记 LOOP-T19-OK",
            "mode": "pull",
            "idempotency_key": "chatgpt-workbuddy-loop-0001",
        },
        13,
    )
    assert replay["idempotent_replay"] is True
    assert replay["task"]["id"] == task_id

    # Distinct credentials remain non-interchangeable.
    denied = loop_client.post(
        "/integrations/workbuddy/v1/presence",
        headers={"X-WorkBuddy-Connector-Token": CHATGPT_TOKEN},
    )
    assert denied.status_code == 405  # endpoint is GET-only; no mutation was attempted
    wrong_auth = loop_client.get(
        "/integrations/workbuddy/v1/presence",
        headers={"X-WorkBuddy-Connector-Token": CHATGPT_TOKEN},
    )
    assert wrong_auth.status_code == 403
