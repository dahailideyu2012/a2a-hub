"""T92-T98 diagnostics recovery envelope API tests."""
from __future__ import annotations
import a2a_hub.server as server
TOKEN = "test-workbuddy-recovery-token-0123456789"
BASE = "/integrations/workbuddy/v1"

def _configure(monkeypatch, tmp_path):
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    monkeypatch.setattr(settings, "workbuddy_heartbeat_interval_seconds", 20)
    monkeypatch.setattr(settings, "workbuddy_presence_ttl_seconds", 90)

def _headers():
    return {"X-WorkBuddy-Connector-Token": TOKEN}

def test_no_handshake_diagnostics_include_safe_recovery_envelope(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    response = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics_version"] == 3
    assert body["connection_state"] == "offline"
    assert body["recovery"]["action"] == "reconnect"
    assert body["recovery"]["retry_allowed"] is True
    assert body["recovery"]["automatic_actions_enabled"] is False
    assert TOKEN not in response.text and "lease_token" not in response.text

def test_online_diagnostics_report_no_recovery_action(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    handshake = client.post(f"{BASE}/handshake", headers=_headers(), json={
        "connector_id": "workbuddy-personal", "protocol_version": "1.0",
        "client_version": "recovery-test", "capabilities": ["heartbeat"],
    })
    assert handshake.status_code == 200
    heartbeat = client.post(f"{BASE}/heartbeat", headers=_headers(), json={
        "connector_id": "workbuddy-personal", "heartbeat_id": "heartbeat-recovery-0001",
        "heartbeat_seq": 1, "status": "idle", "current_task_id": None,
    })
    assert heartbeat.status_code == 200
    response = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert response.status_code == 200
    assert response.json()["recovery"]["action"] == "none"
    assert response.json()["recovery"]["retry_allowed"] is False

def test_diagnostics_recovery_remains_authenticated(client, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    assert client.get(f"{BASE}/diagnostics").status_code == 401
