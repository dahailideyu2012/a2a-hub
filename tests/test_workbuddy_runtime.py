"""T57-T63 WorkBuddy connector resilience and diagnostics regression tests."""
from __future__ import annotations
import sqlite3
import pytest
from a2a_hub.workbuddy_runtime import (
    ConnectorRuntimeStore, SUPPORTED_PROTOCOL_VERSIONS,
    classify_connection_age, negotiate_protocol, reconnect_delay,
)


def test_runtime_state_survives_process_restart_without_credentials(tmp_path):
    path = tmp_path / "connector-state.sqlite3"
    first = ConnectorRuntimeStore(path, "workbuddy-a")
    first.record_handshake("1.0", "client-1", "2026-11-25T00:00:00Z")
    first.record_heartbeat("heartbeat-0001", received_at=1000.0)
    restored = ConnectorRuntimeStore(path, "workbuddy-a").load()
    assert restored["protocol_version"] == "1.0"
    assert restored["client_version"] == "client-1"
    assert restored["last_heartbeat_id"] == "heartbeat-0001"
    assert restored["last_heartbeat_at"] == 1000.0
    assert restored["connection_state"] == "online"
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(connector_runtime_state)")}
    assert not ({"token", "secret", "lease_token", "password"} & columns)


def test_runtime_state_rejects_invalid_connector_and_heartbeat_ids(tmp_path):
    with pytest.raises(ValueError, match="identity"):
        ConnectorRuntimeStore(tmp_path / "state.db", "../other")
    store = ConnectorRuntimeStore(tmp_path / "state.db", "workbuddy-a")
    with pytest.raises(ValueError, match="heartbeat"):
        store.record_heartbeat("bad id")


@pytest.mark.parametrize(("age", "expected"), [
    (None, "offline"), (0, "online"), (60, "online"),
    (60.1, "degraded"), (90, "degraded"), (90.1, "offline"),
])
def test_connection_health_ttl_boundaries(age, expected):
    assert classify_connection_age(age, heartbeat_interval_seconds=30,
                                    presence_ttl_seconds=90)["state"] == expected


def test_reconnect_backoff_is_exponential_bounded_and_jittered():
    assert reconnect_delay(0, random_value=0.5) == 1
    assert reconnect_delay(1, random_value=0.5) == 2
    assert reconnect_delay(2, random_value=0.0) == pytest.approx(3.2)
    assert reconnect_delay(20, random_value=1.0) == 60
    with pytest.raises(ValueError):
        reconnect_delay(-1)
    with pytest.raises(ValueError):
        reconnect_delay(1, random_value=1.1)


def test_reconnect_attempt_counter_is_durable(tmp_path):
    path = tmp_path / "reconnect.db"
    store = ConnectorRuntimeStore(path, "workbuddy-a")
    assert store.record_reconnect_attempt() == 1
    assert store.record_reconnect_attempt() == 2
    assert ConnectorRuntimeStore(path, "workbuddy-a").load()["reconnect_attempt"] == 2


def test_protocol_negotiation_is_explicit_and_fail_closed():
    assert SUPPORTED_PROTOCOL_VERSIONS == ("1.0",)
    assert negotiate_protocol("1.0") == "1.0"
    for version in ("2.0", "1.1", "v1", ""):
        with pytest.raises(ValueError):
            negotiate_protocol(version)


def test_runtime_state_isolated_by_connector_identity(tmp_path):
    path = tmp_path / "multi-connector.db"
    first = ConnectorRuntimeStore(path, "workbuddy-a")
    second = ConnectorRuntimeStore(path, "workbuddy-b")
    first.record_handshake("1.0", "client-a")
    first.record_heartbeat("heartbeat-a-0001", received_at=100.0)
    second.record_handshake("1.0", "client-b")
    second.record_heartbeat("heartbeat-b-0001", received_at=200.0)
    assert first.load()["last_heartbeat_id"] == "heartbeat-a-0001"
    assert second.load()["last_heartbeat_id"] == "heartbeat-b-0001"
    assert first.load()["client_version"] == "client-a"
    assert second.load()["client_version"] == "client-b"


def test_diagnostics_endpoint_is_authenticated_redacted_and_reports_health(client, monkeypatch, tmp_path):
    import a2a_hub.server as server
    token = "test-workbuddy-connector-token-0123456789"
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    headers = {"X-WorkBuddy-Connector-Token": token}
    absent = client.get("/integrations/workbuddy/v1/diagnostics", headers=headers)
    assert absent.status_code == 200
    assert absent.json()["connection_state"] == "offline"
    handshake = client.post("/integrations/workbuddy/v1/handshake", headers=headers,
        json={"connector_id": "workbuddy-personal", "protocol_version": "1.0",
              "client_version": "test-client", "capabilities": ["heartbeat"]})
    assert handshake.status_code == 200
    heartbeat = client.post("/integrations/workbuddy/v1/heartbeat", headers=headers,
        json={"connector_id": "workbuddy-personal", "heartbeat_id": "heartbeat-diag-0001",
              "status": "idle", "current_task_id": None})
    assert heartbeat.status_code == 200
    result = client.get("/integrations/workbuddy/v1/diagnostics", headers=headers)
    assert result.status_code == 200
    body = result.json()
    assert body["connection_state"] == "online"
    assert body["protocol_version"] == "1.0"
    assert body["last_heartbeat_id"] == "heartbeat-diag-0001"
    assert body["supported_protocol_versions"] == ["1.0"]
    assert token not in result.text
    assert "lease_token" not in result.text


def test_handshake_rejects_unsupported_protocol_with_426(client, monkeypatch, tmp_path):
    import a2a_hub.server as server
    token = "test-workbuddy-connector-token-0123456789"
    settings = server.hub.settings
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db"))
    response = client.post("/integrations/workbuddy/v1/handshake",
        headers={"X-WorkBuddy-Connector-Token": token},
        json={"connector_id": "workbuddy-personal", "protocol_version": "2.0",
              "client_version": "test-client", "capabilities": ["heartbeat"]})
    assert response.status_code == 426
    assert response.headers["x-workbuddy-supported-protocols"] == "1.0"


def test_runtime_heartbeat_requires_prior_handshake(tmp_path):
    store = ConnectorRuntimeStore(tmp_path / "no-handshake.db", "workbuddy-a")
    with pytest.raises(ValueError, match="handshake"):
        store.record_heartbeat("heartbeat-0001")
