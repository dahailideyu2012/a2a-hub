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



@pytest.mark.parametrize(
    ("raw", "default", "maximum", "expected"),
    [
        (None, 30, 120, 30),
        ("2.5", 30, 120, 2.5),
        ("120", 30, 120, 120),
        (None, 15, 60, 15),
        ("60", 15, 60, 60),
    ],
)
def test_http_timeout_accepts_defaults_and_bounded_values(
    monkeypatch, raw, default, maximum, expected
):
    env_name = "A2A_WORKBUDDY_TEST_TIMEOUT"
    if raw is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, raw)

    assert server._http_timeout(env_name, default, maximum) == expected


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "120.1", "nan", "inf", "-inf"])
def test_http_timeout_rejects_invalid_or_unbounded_values(monkeypatch, raw):
    env_name = "A2A_WORKBUDDY_TEST_TIMEOUT"
    monkeypatch.setenv(env_name, raw)

    with pytest.raises(RuntimeError, match="A2A_WORKBUDDY_TEST_TIMEOUT"):
        server._http_timeout(env_name, 30, 120)


@pytest.mark.parametrize(
    ("path", "env_name", "env_value", "expected"),
    [
        ("presence", "A2A_WORKBUDDY_GET_TIMEOUT_SECONDS", "4.5", 4.5),
        ("heartbeat", "A2A_WORKBUDDY_POST_TIMEOUT_SECONDS", "8", 8.0),
    ],
)
def test_http_requests_apply_configured_timeout(
    monkeypatch, path, env_name, env_value, expected
):
    monkeypatch.setenv(env_name, env_value)
    observed = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"accepted": true, "connector_id": "workbuddy-personal", "online": true}'

    class FakeOpener:
        def open(self, request, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *_args: FakeOpener())

    if path == "presence":
        server._get(path)
    else:
        server._post(path, {"status": "idle"})

    assert observed["timeout"] == expected
    assert observed["request"].full_url.endswith("/" + path)
