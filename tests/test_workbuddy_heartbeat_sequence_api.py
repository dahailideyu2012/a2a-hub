"""HTTP-level heartbeat sequence guard compatibility tests."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-sequence-token-0123456789"
BASE = "/integrations/workbuddy/v1"


def _setup(monkeypatch, tmp_path):
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))


def _headers():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def test_heartbeat_sequence_rejects_reordered_requests_and_reports_receipt(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    handshake = client.post(f"{BASE}/handshake", headers=_headers(), json={
        "connector_id": "workbuddy-personal", "protocol_version": "1.0",
        "client_version": "sequence-test", "capabilities": ["heartbeat"],
    })
    assert handshake.status_code == 200

    def send(seq, heartbeat_id):
        return client.post(f"{BASE}/heartbeat", headers=_headers(), json={
            "connector_id": "workbuddy-personal", "heartbeat_id": heartbeat_id,
            "heartbeat_seq": seq, "status": "busy", "current_task_id": "task-safe",
        })

    accepted = send(7, "heartbeat-seq-0007")
    assert accepted.status_code == 200
    assert accepted.json()["heartbeat_seq"] == 7
    stale = send(6, "heartbeat-seq-0006")
    assert stale.status_code == 409
    replay = send(7, "heartbeat-seq-replay")
    assert replay.status_code == 409
    next_one = send(8, "heartbeat-seq-0008")
    assert next_one.status_code == 200
    diagnostics = client.get(f"{BASE}/diagnostics", headers=_headers())
    assert diagnostics.status_code == 200
    assert diagnostics.json()["last_heartbeat_seq"] == 8


def test_heartbeat_sequence_requires_strict_positive_integer(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    handshake = client.post(f"{BASE}/handshake", headers=_headers(), json={
        "connector_id": "workbuddy-personal", "protocol_version": "1.0",
        "client_version": "strict-sequence-test", "capabilities": ["heartbeat"],
    })
    assert handshake.status_code == 200
    for invalid in (0, -1, True, 1.5, "2"):
        response = client.post(f"{BASE}/heartbeat", headers=_headers(), json={
            "connector_id": "workbuddy-personal", "heartbeat_id": "heartbeat-invalid",
            "heartbeat_seq": invalid, "status": "idle",
        })
        assert response.status_code == 422
