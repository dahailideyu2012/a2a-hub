"""Durable WorkBuddy presence API tests."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-connector-token-0123456789"
BASE = "/integrations/workbuddy/v1"


def configure(monkeypatch, tmp_path, ttl=90):
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_allowed_agents", "echo-a,workbuddy")
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_lease_seconds", 120)
    monkeypatch.setattr(server.hub.settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    monkeypatch.setattr(server.hub.settings, "workbuddy_presence_ttl_seconds", ttl)


def auth():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def handshake(client):
    return client.post(
        f"{BASE}/handshake",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "protocol_version": "1.0",
            "client_version": "5.11.3.0",
            "capabilities": ["claim", "heartbeat"],
        },
    )


def test_presence_is_offline_until_handshake(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    response = client.get(f"{BASE}/presence", headers=auth())
    assert response.status_code == 200
    assert response.json()["online"] is False
    assert response.json()["status"] == "offline"
    assert response.json()["reason"] == "handshake_not_seen"


def test_heartbeat_requires_a_registered_handshake(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    response = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "heartbeat_id": "heartbeat-0001",
            "status": "idle",
        },
    )
    assert response.status_code == 409


def test_handshake_persists_presence_and_negotiated_capabilities(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    response = handshake(client)
    assert response.status_code == 200
    assert response.json()["presence_ttl_seconds"] == 90

    # A new store instance is created for every request; state must survive it.
    presence = client.get(f"{BASE}/presence", headers=auth())
    assert presence.status_code == 200
    body = presence.json()
    assert body["online"] is True
    assert body["status"] == "idle"
    assert body["connector_id"] == "workbuddy-personal"
    assert body["client_version"] == "5.11.3.0"
    assert body["capabilities"] == ["claim", "heartbeat"]
    assert body["last_heartbeat_id"] is None


def test_heartbeat_updates_durable_status_and_task(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert handshake(client).status_code == 200
    receipt = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "heartbeat_id": "heartbeat-0002",
            "status": "busy",
            "current_task_id": "task-123",
        },
    )
    assert receipt.status_code == 200
    assert receipt.json()["receipt_id"] == "heartbeat-0002"

    presence = client.get(f"{BASE}/presence", headers=auth()).json()
    assert presence["online"] is True
    assert presence["status"] == "busy"
    assert presence["current_task_id"] == "task-123"
    assert presence["last_heartbeat_id"] == "heartbeat-0002"


def test_presence_expires_to_offline_after_ttl(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, ttl=90)
    clock = {"now": 1000.0}
    monkeypatch.setattr("a2a_hub.workbuddy_presence.time.time", lambda: clock["now"])
    assert handshake(client).status_code == 200
    receipt = client.post(
        f"{BASE}/heartbeat",
        headers=auth(),
        json={
            "connector_id": "workbuddy-personal",
            "heartbeat_id": "heartbeat-0003",
            "status": "busy",
            "current_task_id": "task-456",
        },
    )
    assert receipt.status_code == 200

    clock["now"] = 1091.0
    body = client.get(f"{BASE}/presence", headers=auth()).json()
    assert body["online"] is False
    assert body["status"] == "offline"
    assert body["last_status"] == "busy"
    assert body["current_task_id"] is None
    assert body["seconds_since_seen"] == 91.0


def test_presence_endpoint_requires_connector_auth(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    missing = client.get(f"{BASE}/presence")
    wrong = client.get(f"{BASE}/presence", headers={"X-WorkBuddy-Connector-Token": "x" * 40})
    assert missing.status_code == 401
    assert wrong.status_code == 403
