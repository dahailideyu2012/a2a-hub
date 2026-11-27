from __future__ import annotations

import time

import pytest

import workbuddy_mcp_server as server


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    with server.LEASES_LOCK:
        server.LEASES.clear()
    server.HANDSHAKE_READY = False
    server.HANDSHAKE_CACHE_KEY = None
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "workbuddy-personal")
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "x" * 48)
    yield
    server._cleanup_local_session()


def seed_lease(task_id="retry-task"):
    with server.LEASES_LOCK:
        server.LEASES[task_id] = {
            "lease_token": "private-token",
            "lease_version": 7,
            "lease_expires_at": time.time() + 120,
        }


def test_shutdown_cleanup_discards_all_process_local_lease_and_handshake_state():
    seed_lease()
    server.HANDSHAKE_READY = True
    server.HANDSHAKE_CACHE_KEY = ("https://hub.example.test", "workbuddy-personal", "fingerprint")

    server._cleanup_local_session()

    assert server.LEASES == {}
    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None


def test_progress_retries_only_transient_network_failures_with_identical_payload(monkeypatch):
    seed_lease()
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if len(calls) < 3:
            raise RuntimeError("无法连接 Hub：TimeoutError")
        return {"accepted": True, "sequence": payload["sequence"]}

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)

    result = server.call_tool("workbuddy_progress_task", {
        "task_id": "retry-task", "sequence": 4, "message": "checkpoint",
    })

    assert result["isError"] is False
    assert len(calls) == 3
    assert calls[0] == calls[1] == calls[2]
    assert calls[0][1]["sequence"] == 4
    assert calls[0][1]["message"] == "checkpoint"


def test_result_retry_reuses_stable_result_key_and_cleans_lease_once(monkeypatch):
    seed_lease()
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if len(calls) == 1:
            raise RuntimeError("Hub HTTP 503: temporary")
        return {"accepted": True, "result_key": payload["result_key"]}

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)

    result = server.call_tool("workbuddy_submit_result", {
        "task_id": "retry-task", "state": "completed", "output": "done",
    })

    assert result["isError"] is False
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][1]["result_key"] == "retry-task:workbuddy-result-v1"
    assert "retry-task" not in server.LEASES


@pytest.mark.parametrize("error", [
    "Hub HTTP 400: invalid",
    "Hub HTTP 401: unauthorized",
    "Hub HTTP 409: conflict",
])
def test_progress_does_not_retry_non_transient_http_errors(monkeypatch, error):
    seed_lease()
    calls = []

    def fake_post(path, payload):
        calls.append(path)
        raise RuntimeError(error)

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)

    result = server.call_tool("workbuddy_progress_task", {
        "task_id": "retry-task", "sequence": 1, "message": "x",
    })

    assert result["isError"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("tool_name,args,path", [
    ("workbuddy_start_task", {"task_id": "retry-task"}, "/start"),
    ("workbuddy_renew_task", {"task_id": "retry-task"}, "/renew"),
])
def test_non_idempotent_start_and_renew_are_never_automatically_retried(
    monkeypatch, tool_name, args, path
):
    seed_lease()
    calls = []

    def fake_post(request_path, payload):
        calls.append(request_path)
        raise RuntimeError("Hub HTTP 503: temporary")

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)

    result = server.call_tool(tool_name, args)

    assert result["isError"] is True
    assert len(calls) == 1
    assert calls[0].endswith(path)


def test_process_exit_registers_local_session_cleanup(monkeypatch):
    import importlib

    registered_callbacks = []

    monkeypatch.setattr(
        server.atexit,
        "register",
        lambda callback, *args, **kwargs: registered_callbacks.append(callback),
    )
    importlib.reload(server)

    assert server._cleanup_local_session in registered_callbacks

    seed_lease()
    server.HANDSHAKE_READY = True
    server.HANDSHAKE_CACHE_KEY = ("https://hub.example.test", "workbuddy-personal", "fingerprint")

    registered_callbacks[-1]()

    assert server.LEASES == {}
    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None

