"""T71-T77 integration tests for the WorkBuddy handshake capability contract."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-capability-token-0123456789"


def _configure(monkeypatch):
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")


def _handshake(client, capabilities):
    return client.post(
        "/integrations/workbuddy/v1/handshake",
        headers={"X-WorkBuddy-Connector-Token": TOKEN},
        json={
            "connector_id": "workbuddy-personal",
            "protocol_version": "1.0",
            "client_version": "test-client",
            "capabilities": capabilities,
        },
    )


def test_handshake_reports_unknown_capabilities_without_enabling_them(client, monkeypatch):
    _configure(monkeypatch)
    response = _handshake(client, ["future_feature", "claim", "heartbeat"])
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"] == ["claim", "heartbeat"]
    assert body["unsupported_capabilities"] == ["future_feature"]


def test_handshake_rejects_duplicate_capabilities(client, monkeypatch):
    _configure(monkeypatch)
    response = _handshake(client, ["claim", "claim"])
    assert response.status_code == 422


def test_handshake_rejects_malformed_capability_names(client, monkeypatch):
    _configure(monkeypatch)
    response = _handshake(client, ["claim", "../admin"])
    assert response.status_code == 422


def test_handshake_rejects_more_than_twenty_capabilities(client, monkeypatch):
    _configure(monkeypatch)
    response = _handshake(client, [f"feature_{index}" for index in range(21)])
    assert response.status_code == 422
