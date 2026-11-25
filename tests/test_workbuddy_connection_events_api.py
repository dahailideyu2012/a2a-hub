"""T99-T105 persisted WorkBuddy connection event API tests."""
from __future__ import annotations

import a2a_hub.server as server

TOKEN = "test-workbuddy-events-token-0123456789"
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


def headers():
    return {"X-WorkBuddy-Connector-Token": TOKEN}


def report_error(client, code="timeout"):
    return client.post(
        f"{BASE}/connection-events/error",
        headers=headers(),
        json={"connector_id": "workbuddy-personal", "error_code": code},
    )


def report_attempt(client):
    return client.post(
        f"{BASE}/connection-events/reconnect-attempt",
        headers=headers(),
        json={"connector_id": "workbuddy-personal"},
    )


def diagnostics(client):
    return client.get(f"{BASE}/diagnostics", headers=headers())


def handshake(client):
    return client.post(f"{BASE}/handshake", headers=headers(), json={
        "connector_id": "workbuddy-personal",
        "protocol_version": "1.0",
        "client_version": "events-test",
        "capabilities": ["heartbeat"],
    })


def test_fatal_error_report_before_handshake_reaches_diagnostics(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert report_error(client, "authentication_failed").status_code == 200
    body = diagnostics(client).json()
    assert body["diagnostics_version"] == 3
    assert body["connection_state"] == "offline"
    assert body["last_error_code"] == "authentication_failed"
    assert body["last_error_at"] is not None
    assert body["recovery"]["action"] == "operator_intervention"
    assert body["recovery"]["reason"] == "authentication_failed"
    assert body["recovery"]["automatic_actions_enabled"] is False
    assert TOKEN not in str(body) and "lease_token" not in str(body)


def test_retry_attempt_counter_is_persisted_and_budget_is_enforced(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert report_error(client, "timeout").status_code == 200
    for expected in range(1, 9):
        response = report_attempt(client)
        assert response.status_code == 200
        assert response.json()["reconnect_attempt"] == expected
    body = diagnostics(client).json()
    assert body["reconnect_attempt"] == 8
    assert body["recovery"]["action"] == "operator_intervention"
    assert body["recovery"]["reason"] == "retry_budget_exhausted"
    assert body["recovery"]["attempts_remaining"] == 0


def test_transient_error_is_visible_and_retryable_before_budget_exhaustion(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert report_error(client, "network_unreachable").status_code == 200
    assert report_attempt(client).status_code == 200
    body = diagnostics(client).json()
    assert body["last_error_code"] == "network_unreachable"
    assert body["reconnect_attempt"] == 1
    assert body["recovery"]["action"] == "reconnect"
    assert body["recovery"]["reason"] == "network_unreachable"
    assert body["recovery"]["attempts_remaining"] == 7


def test_handshake_clears_prior_error_and_retry_history(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert report_error(client, "protocol_mismatch").status_code == 200
    assert report_attempt(client).status_code == 200
    assert handshake(client).status_code == 200
    body = diagnostics(client).json()
    assert body["last_error_code"] is None
    assert body["last_error_at"] is None
    assert body["reconnect_attempt"] == 0
    assert body["recovery"]["action"] == "reconnect"


def test_successful_heartbeat_clears_error_and_retry_history(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert handshake(client).status_code == 200
    assert report_error(client, "timeout").status_code == 200
    assert report_attempt(client).status_code == 200
    heartbeat = client.post(f"{BASE}/heartbeat", headers=headers(), json={
        "connector_id": "workbuddy-personal",
        "heartbeat_id": "heartbeat-events-0001",
        "heartbeat_seq": 1,
        "status": "idle",
        "current_task_id": None,
    })
    assert heartbeat.status_code == 200
    body = diagnostics(client).json()
    assert body["last_error_code"] is None
    assert body["reconnect_attempt"] == 0
    assert body["recovery"]["action"] == "none"


def test_event_endpoints_are_authenticated_and_identity_bound(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    assert client.post(f"{BASE}/connection-events/error",
        json={"connector_id": "workbuddy-personal", "error_code": "timeout"}).status_code == 401
    wrong = client.post(f"{BASE}/connection-events/error", headers=headers(),
        json={"connector_id": "another-connector", "error_code": "timeout"})
    assert wrong.status_code == 403


def test_error_reporting_accepts_only_allowlisted_codes_and_strict_shape(client, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    invalid = report_error(client, "password=secret")
    assert invalid.status_code == 422
    extra = client.post(f"{BASE}/connection-events/error", headers=headers(), json={
        "connector_id": "workbuddy-personal", "error_code": "timeout", "raw_error": "secret",
    })
    assert extra.status_code == 422
    assert diagnostics(client).json()["last_error_code"] is None
