"""Authenticated WorkBuddy connector handshake and heartbeat receipt tests."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-connector-token-0123456789"
BASE = "/integrations/workbuddy/v1"


def configure(monkeypatch):
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_allowed_agents", "workbuddy,echo-a")
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_lease_seconds", 120)


def auth():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def test_handshake_requires_dedicated_connector_token(client, monkeypatch):
    configure(monkeypatch)
    response = client.post(
        f"{BASE}/handshake",
        json={"connector_id": "workbuddy-personal", "protocol_version": "1.0"},
    )
    assert response.status_code == 401


def test_handshake_rejects_wrong_token(client, monkeypatch):
    configure(monkeypatch)
    response = client.post(
        f"{BASE}/handshake",
        headers={"X-WorkBuddy-Connector-Token": "x" * 40},
        json={"connector_id": "workbuddy-personal", "protocol_version": "1.0"},
    )
    assert response.status_code == 403


def test_handshake_rejects_connector_identity_spoofing(client, monkeypatch):
    configure(monkeypatch)
    response = client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={"connector_id": "another-device", "protocol_version": "1.0"},
    )
    assert response.status_code == 403


def test_handshake_negotiates_only_supported_capabilities(client, monkeypatch):
    configure(monkeypatch)
    response = client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "protocol_version": "1.0",
            "client_version": "5.11.3.0",
            "capabilities": ["claim", "progress", "unknown"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["connector_id"] == "workbuddy-personal"
    assert body["protocol_version"] == "1.0"
    assert body["client_version"] == "5.11.3.0"
    assert body["allowed_agent_ids"] == ["echo-a", "workbuddy"]
    assert body["capabilities"] == ["claim", "progress"]
    assert body["heartbeat_interval_seconds"] == 30
    assert body["lease_seconds"] == 120
    assert TOKEN not in response.text


def test_handshake_fails_closed_without_agent_allowlist(client, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_allowed_agents", "")
    response = client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={"connector_id": "workbuddy-personal", "protocol_version": "1.0"},
    )
    assert response.status_code == 503


def test_handshake_rejects_unsupported_protocol_and_unknown_fields(client, monkeypatch):
    configure(monkeypatch)
    unsupported = client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={"connector_id": "workbuddy-personal", "protocol_version": "2.0"},
    )
    assert unsupported.status_code == 422
    extra = client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={"connector_id": "workbuddy-personal", "protocol_version": "1.0", "admin": True},
    )
    assert extra.status_code == 422


def test_heartbeat_returns_authenticated_status_receipt(client, monkeypatch):
    configure(monkeypatch)
    response = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "heartbeat_id": "heartbeat-0001",
            "status": "busy",
            "current_task_id": "task-sandbox-0001",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["connector_id"] == "workbuddy-personal"
    assert body["receipt_id"] == "heartbeat-0001"
    assert body["status"] == "busy"
    assert body["current_task_id"] == "task-sandbox-0001"
    assert body["next_heartbeat_seconds"] == 30
    assert "lease_token" not in response.text
    assert TOKEN not in response.text


def test_heartbeat_rejects_identity_mismatch_and_invalid_status(client, monkeypatch):
    configure(monkeypatch)
    mismatch = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={"connector_id": "other-device", "heartbeat_id": "heartbeat-0002", "status": "idle"},
    )
    assert mismatch.status_code == 403
    invalid = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={"connector_id": "workbuddy-personal", "heartbeat_id": "heartbeat-0003", "status": "root"},
    )
    assert invalid.status_code == 422
