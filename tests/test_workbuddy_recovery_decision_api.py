"""HTTP contract tests for operator-in-the-loop WorkBuddy recovery decisions."""
from __future__ import annotations

import json
import re

import a2a_hub.server as server

TOKEN = "test-workbuddy-recovery-decision-token-012345"
BASE = "/integrations/workbuddy/v1"


def configure(monkeypatch, tmp_path):
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", TOKEN)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    monkeypatch.setattr(settings, "workbuddy_rate_limit_db_path", str(tmp_path / "rate.db"))
    monkeypatch.setattr(settings, "workbuddy_heartbeat_interval_seconds", 20)
    monkeypatch.setattr(settings, "workbuddy_presence_ttl_seconds", 90)
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 60)


def headers():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def diagnostics(client, auth=True):
    return client.get(
        f"{BASE}/diagnostics",
        headers=headers() if auth else {},
    )


def decide(client, snapshot_id, disposition="acknowledged", connector_id="workbuddy-personal", extra=None):
    body = {
        "connector_id": connector_id,
        "snapshot_id": snapshot_id,
        "disposition": disposition,
    }
    if extra:
        body.update(extra)
    return client.post(f"{BASE}/recovery/decision", headers=headers(), json=body)


def handshake(client):
    return client.post(f"{BASE}/handshake", headers=headers(), json={
        "connector_id": "workbuddy-personal",
        "protocol_version": "1.0",
        "client_version": "recovery-decision-test",
        "capabilities": ["heartbeat"],
    })


def heartbeat(client, sequence=1):
    return client.post(f"{BASE}/heartbeat", headers=headers(), json={
        "connector_id": "workbuddy-personal",
        "heartbeat_id": f"recovery-heartbeat-{sequence:04d}",
        "heartbeat_seq": sequence,
        "status": "idle",
        "current_task_id": None,
    })


def test_diagnostics_v4_keeps_legacy_fields_and_stable_snapshot_id(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    first = diagnostics(client)
    second = diagnostics(client)

    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["diagnostics_version"] == 4
    assert body["connection_state"] == "offline"
    assert body["last_error_code"] is None
    assert body["last_error_at"] is None
    assert body["reconnect_attempt"] == 0
    assert body["recovery"]["automatic_actions_enabled"] is False
    assert body["operator_decision"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", body["recovery_snapshot_id"])
    assert second.json()["recovery_snapshot_id"] == body["recovery_snapshot_id"]
    assert TOKEN not in json.dumps(body)
    assert "lease_token" not in json.dumps(body)


def test_recovery_decision_requires_auth_identity_and_strict_payload(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]

    missing = client.post(f"{BASE}/recovery/decision", json={
        "connector_id": "workbuddy-personal",
        "snapshot_id": snapshot_id,
        "disposition": "acknowledged",
    })
    assert missing.status_code == 401
    assert decide(client, snapshot_id, connector_id="other-connector").status_code == 403
    assert decide(client, snapshot_id, disposition="execute").status_code == 422
    assert decide(client, "not-a-snapshot").status_code == 422
    assert decide(client, snapshot_id, extra={"raw_note": "secret"}).status_code == 422


def test_decision_receipt_is_persisted_and_exact_replay_is_idempotent(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]

    response = decide(client, snapshot_id, "acknowledged")
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["accepted"] is True
    assert receipt["connector_id"] == "workbuddy-personal"
    assert receipt["snapshot_id"] == snapshot_id
    assert receipt["disposition"] == "acknowledged"
    assert receipt["action_executed"] is False
    assert receipt["automatic_actions_enabled"] is False
    assert TOKEN not in json.dumps(receipt)
    assert "lease_token" not in json.dumps(receipt)

    replay = decide(client, snapshot_id, "acknowledged")
    assert replay.status_code == 200
    assert replay.json()["recorded_at"] == receipt["recorded_at"]

    conflict = decide(client, snapshot_id, "dismissed")
    assert conflict.status_code == 409

    body = diagnostics(client).json()
    assert body["operator_decision"]["disposition"] == "acknowledged"
    assert body["operator_decision"]["action_executed"] is False


def test_state_change_invalidates_decision_and_rejects_old_snapshot(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]
    assert decide(client, snapshot_id, "deferred").status_code == 200

    error = client.post(f"{BASE}/connection-events/error", headers=headers(), json={
        "connector_id": "workbuddy-personal",
        "error_code": "timeout",
    })
    assert error.status_code == 200
    current = diagnostics(client).json()
    assert current["recovery_snapshot_id"] != snapshot_id
    assert current["operator_decision"] is None
    assert current["last_error_code"] == "timeout"
    assert decide(client, snapshot_id, "acknowledged").status_code == 409


def test_accepted_heartbeat_invalidates_decision_snapshot(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert handshake(client).status_code == 200
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]
    assert decide(client, snapshot_id, "acknowledged").status_code == 200

    assert heartbeat(client).status_code == 200
    current = diagnostics(client).json()
    assert current["recovery_snapshot_id"] != snapshot_id
    assert current["operator_decision"] is None
    assert current["connection_state"] == "online"


def test_recovery_decision_uses_existing_connector_rate_limit(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(server.hub.settings, "workbuddy_connector_rate_limit", 1)
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]
    limited = decide(client, snapshot_id, "acknowledged")
    assert limited.status_code == 429
    assert diagnostics(client).status_code == 429


def test_recovery_decision_audit_is_closed_and_redacted(client, monkeypatch, tmp_path, caplog):
    configure(monkeypatch, tmp_path)
    snapshot_id = diagnostics(client).json()["recovery_snapshot_id"]
    with caplog.at_level("INFO", logger="a2a_hub.audit.workbuddy"):
        response = decide(client, snapshot_id, "dismissed")
    assert response.status_code == 200
    records = [record.getMessage() for record in caplog.records if record.name == "a2a_hub.audit.workbuddy"]
    assert any('"event":"recovery_decision"' in item for item in records)
    assert all(TOKEN not in item and "lease_token" not in item for item in records)
    assert all(snapshot_id not in item for item in records)
