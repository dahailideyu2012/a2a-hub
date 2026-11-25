"""T85-T91 configurable WorkBuddy heartbeat policy API regression tests."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-policy-token-0123456789"
BASE = "/integrations/workbuddy/v1"


def _configure(monkeypatch, tmp_path, *, interval=20, ttl=90):
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    monkeypatch.setattr(settings, "workbuddy_heartbeat_interval_seconds", interval)
    monkeypatch.setattr(settings, "workbuddy_presence_ttl_seconds", ttl)


def _headers():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def _handshake(client):
    return client.post(f"{BASE}/handshake", headers=_headers(), json={
        "connector_id": "workbuddy-personal",
        "protocol_version": "1.0",
        "client_version": "heartbeat-policy-test",
        "capabilities": ["heartbeat"],
    })


def test_handshake_and_diagnostics_use_configured_interval(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, interval=20, ttl=90)
    handshake = _handshake(client)
    assert handshake.status_code == 200
    assert handshake.json()["heartbeat_interval_seconds"] == 20
    absent = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert absent.status_code == 200
    assert absent.json()["heartbeat_interval_seconds"] == 20
    assert absent.json()["presence_ttl_seconds"] == 90


def test_heartbeat_receipt_uses_configured_next_interval(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, interval=45, ttl=120)
    assert _handshake(client).status_code == 200
    response = client.post(f"{BASE}/heartbeat", headers=_headers(), json={
        "connector_id": "workbuddy-personal",
        "heartbeat_id": "heartbeat-policy-0001",
        "heartbeat_seq": 1,
        "status": "idle",
        "current_task_id": None,
    })
    assert response.status_code == 200
    assert response.json()["next_heartbeat_seconds"] == 45
    diagnostics = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert diagnostics.status_code == 200
    assert diagnostics.json()["heartbeat_interval_seconds"] == 45


def test_invalid_ttl_shorter_than_interval_fails_closed(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, interval=60, ttl=30)
    response = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert response.status_code == 503
    assert response.json()["detail"] == "WorkBuddy heartbeat interval/TTL configuration is invalid"


def test_interval_upper_and_lower_bounds_are_enforced_by_runtime(client, monkeypatch, tmp_path):
    for interval in (4, 301):
        _configure(monkeypatch, tmp_path, interval=interval, ttl=360)
        response = client.get(f"{BASE}/diagnostics", headers=_headers())
        assert response.status_code == 503


def test_configured_interval_controls_health_classification(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, interval=60, ttl=90)
    assert _handshake(client).status_code == 200
    heartbeat = client.post(f"{BASE}/heartbeat", headers=_headers(), json={
        "connector_id": "workbuddy-personal",
        "heartbeat_id": "heartbeat-policy-0002",
        "status": "idle",
        "current_task_id": None,
    })
    assert heartbeat.status_code == 200
    diagnostics = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert diagnostics.status_code == 200
    assert diagnostics.json()["heartbeat_interval_seconds"] == 60
    assert diagnostics.json()["connection_state"] == "online"
