from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import scripts.workbuddy_sandbox as sandbox

from scripts.workbuddy_sandbox import (
    DEFAULT_PORT,
    connector_check,
    create_env_file,
    load_env_file,
    main,
    validate_port,
)


def test_init_creates_three_distinct_credentials_without_printing_them(tmp_path: Path, capsys):
    target = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(target)
    output = capsys.readouterr().out
    values = load_env_file(target)
    assert values["A2A_WORKBUDDY_SANDBOX_PORT"] == str(DEFAULT_PORT)
    credentials = [
        values["A2A_API_TOKEN"],
        values["A2A_CHATGPT_INGRESS_TOKEN"],
        values["A2A_WORKBUDDY_CONNECTOR_TOKEN"],
    ]
    assert len(set(credentials)) == 3
    assert all(len(value) >= 32 for value in credentials)
    assert all(secret not in output for secret in credentials)
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_init_refuses_to_overwrite_existing_env_file(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text("keep-this-file", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_env_file(target)
    assert target.read_text(encoding="utf-8") == "keep-this-file"


@pytest.mark.parametrize("port", [0, 1023, 65536, -1])
def test_invalid_ports_are_rejected(port: int):
    with pytest.raises(ValueError):
        validate_port(port)


@pytest.mark.parametrize("port", [1024, 18081, 65535])
def test_valid_ports_are_accepted(port: int):
    assert validate_port(port) == port


def test_env_reader_rejects_reused_credentials(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text(
        "A2A_WORKBUDDY_SANDBOX_PORT=18081\n"
        "A2A_API_TOKEN=" + "a" * 48 + "\n"
        "A2A_CHATGPT_INGRESS_TOKEN=" + "a" * 48 + "\n"
        "A2A_WORKBUDDY_CONNECTOR_TOKEN=" + "c" * 48 + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid or reused"):
        load_env_file(target)


def test_env_reader_rejects_duplicate_keys(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text(
        "A2A_WORKBUDDY_SANDBOX_PORT=18081\n"
        "A2A_WORKBUDDY_SANDBOX_PORT=18082\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_env_file(target)


def test_destroy_refuses_without_explicit_confirmation(monkeypatch, capsys):
    compose_calls = []
    monkeypatch.setattr(
        "scripts.workbuddy_sandbox._compose",
        lambda *args: compose_calls.append(args) or 0,
    )

    assert main(["destroy"]) == 2
    assert compose_calls == []
    assert "--confirm-delete-data" in capsys.readouterr().err


def test_destroy_removes_volume_only_after_explicit_confirmation(monkeypatch):
    compose_calls = []
    monkeypatch.setattr(
        "scripts.workbuddy_sandbox._compose",
        lambda *args: compose_calls.append(args) or 0,
    )

    assert main(["destroy", "--confirm-delete-data"]) == 0
    assert compose_calls == [("down", "--volumes")]


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


class _FakeConnection:
    def __init__(self, host, port, timeout, calls, responder):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = calls
        self.responder = responder
        self.request_data = None
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.request_data = {
            "method": method,
            "path": path,
            "body": body,
            "headers": dict(headers or {}),
        }
        self.calls.append(self.request_data)

    def getresponse(self):
        status, payload = self.responder(self.request_data)
        return _FakeResponse(status, payload)

    def close(self):
        self.closed = True


def _install_fake_connector_hub(monkeypatch, responder):
    calls = []
    connections = []

    def factory(host, port, timeout=5):
        connection = _FakeConnection(host, port, timeout, calls, responder)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sandbox.http.client, "HTTPConnection", factory)
    return calls, connections


def test_connector_check_runs_loopback_handshake_presence_heartbeat_without_claim(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(env_file)
    values = load_env_file(env_file)
    heartbeat_ids = []
    presence_reads = 0

    def responder(request):
        nonlocal presence_reads
        path = request["path"]
        if path == sandbox.CONNECTOR_HANDSHAKE_PATH:
            payload = json.loads(request["body"])
            assert payload["connector_id"] == sandbox.SANDBOX_CONNECTOR_ID
            assert payload["capabilities"] == ["heartbeat"]
            return 200, {
                "accepted": True,
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "protocol_version": "1.0",
                "capabilities": ["heartbeat"],
            }
        if path == sandbox.CONNECTOR_PRESENCE_PATH:
            presence_reads += 1
            return 200, {
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "online": True,
                "status": "idle",
                "current_task_id": None,
                "last_heartbeat_id": heartbeat_ids[-1] if heartbeat_ids else None,
            }
        if path == sandbox.CONNECTOR_HEARTBEAT_PATH:
            payload = json.loads(request["body"])
            heartbeat_ids.append(payload["heartbeat_id"])
            assert payload["status"] == "idle"
            assert payload["current_task_id"] is None
            return 200, {
                "accepted": True,
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "receipt_id": payload["heartbeat_id"],
                "status": "idle",
                "current_task_id": None,
            }
        raise AssertionError(f"unexpected sandbox request path: {path}")

    calls, connections = _install_fake_connector_hub(monkeypatch, responder)
    result = connector_check(env_file)

    assert result == {
        "status": "PASS",
        "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
        "protocol_version": "1.0",
        "presence": "online",
        "heartbeat_status": "idle",
        "heartbeat_receipt_persisted": True,
        "task_claimed": False,
        "desktop_process_verified": False,
    }
    assert [(call["method"], call["path"]) for call in calls] == [
        ("POST", sandbox.CONNECTOR_HANDSHAKE_PATH),
        ("GET", sandbox.CONNECTOR_PRESENCE_PATH),
        ("POST", sandbox.CONNECTOR_HEARTBEAT_PATH),
        ("GET", sandbox.CONNECTOR_PRESENCE_PATH),
    ]
    assert presence_reads == 2
    assert all(connection.host == "127.0.0.1" for connection in connections)
    assert all(connection.port == DEFAULT_PORT for connection in connections)
    assert all(connection.closed for connection in connections)
    assert all(
        call["headers"].get("X-WorkBuddy-Connector-Token")
        == values["A2A_WORKBUDDY_CONNECTOR_TOKEN"]
        for call in calls
    )
    assert all("X-ChatGPT-Ingress-Token" not in call["headers"] for call in calls)
    assert all(
        values["A2A_WORKBUDDY_CONNECTOR_TOKEN"].encode("utf-8")
        not in (call["body"] or b"")
        for call in calls
    )
    assert "/claim" not in " ".join(call["path"] for call in calls)
    assert "/tasks/" not in " ".join(call["path"] for call in calls)
    assert values["A2A_WORKBUDDY_CONNECTOR_TOKEN"] not in json.dumps(result)


def test_connector_check_fails_closed_on_handshake_identity_mismatch(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(env_file)

    def responder(_request):
        return 200, {
            "accepted": True,
            "connector_id": "unexpected-connector",
            "protocol_version": "1.0",
            "capabilities": ["heartbeat"],
        }

    calls, connections = _install_fake_connector_hub(monkeypatch, responder)
    with pytest.raises(RuntimeError, match="handshake"):
        connector_check(env_file)
    assert len(calls) == 1
    assert all(connection.closed for connection in connections)


def test_connector_check_requires_heartbeat_receipt_to_be_persisted(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(env_file)
    presence_reads = 0

    def responder(request):
        nonlocal presence_reads
        if request["path"] == sandbox.CONNECTOR_HANDSHAKE_PATH:
            return 200, {
                "accepted": True,
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "protocol_version": "1.0",
                "capabilities": ["heartbeat"],
            }
        if request["path"] == sandbox.CONNECTOR_PRESENCE_PATH:
            presence_reads += 1
            return 200, {
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "online": True,
                "status": "idle",
                "current_task_id": None,
                "last_heartbeat_id": None if presence_reads == 1 else "wrong-receipt",
            }
        if request["path"] == sandbox.CONNECTOR_HEARTBEAT_PATH:
            payload = json.loads(request["body"])
            return 200, {
                "accepted": True,
                "connector_id": sandbox.SANDBOX_CONNECTOR_ID,
                "receipt_id": payload["heartbeat_id"],
                "status": "idle",
                "current_task_id": None,
            }
        raise AssertionError("unexpected connector endpoint")

    calls, connections = _install_fake_connector_hub(monkeypatch, responder)
    with pytest.raises(RuntimeError, match="persist"):
        connector_check(env_file)
    assert len(calls) == 4
    assert all(connection.closed for connection in connections)


def test_connector_check_does_not_echo_token_on_http_error(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(env_file)
    token = load_env_file(env_file)["A2A_WORKBUDDY_CONNECTOR_TOKEN"]

    def responder(_request):
        return 403, {"detail": token}

    calls, connections = _install_fake_connector_hub(monkeypatch, responder)
    with pytest.raises(RuntimeError) as exc_info:
        connector_check(env_file)
    assert "HTTP 403" in str(exc_info.value)
    assert token not in str(exc_info.value)
    assert len(calls) == 1
    assert all(connection.closed for connection in connections)
