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


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "120.1", "nan", "inf", "-inf"])
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

        def __init__(self):
            self.body = b'{"accepted": true, "connector_id": "workbuddy-personal", "online": true}'
            self.offset = 0

        def read(self, limit):
            chunk = self.body[self.offset:self.offset + limit]
            self.offset += len(chunk)
            return chunk

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


class FragmentedResponse:
    def __init__(self, body: bytes, chunk_size: int = 3):
        self.body = body
        self.chunk_size = chunk_size
        self.offset = 0
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        take = min(size, self.chunk_size)
        chunk = self.body[self.offset:self.offset + take]
        self.offset += len(chunk)
        return chunk


def test_read_limited_reassembles_short_reads_until_eof():
    body = b'{"accepted":true,"value":"fragmented"}'
    response = FragmentedResponse(body, chunk_size=2)

    assert server._read_limited(response, len(body), "test response") == body
    assert response.offset == len(body)
    assert len(response.read_sizes) > 2
    assert all(size <= 16 * 1024 for size in response.read_sizes)


def test_read_limited_rejects_limit_plus_one_without_unbounded_reading():
    response = FragmentedResponse(b"1234567", chunk_size=2)

    with pytest.raises(RuntimeError, match="6 字节限制"):
        server._read_limited(response, 6, "test response")

    assert response.offset == 7


def test_read_limited_rejects_non_byte_stream():
    class InvalidResponse:
        def read(self, _size):
            return "not-bytes"

    with pytest.raises(RuntimeError, match="不是有效的字节流"):
        server._read_limited(InvalidResponse(), 32, "test response")


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("_get", "presence", None),
    ("_post", "heartbeat", {"status": "idle"}),
])
def test_http_methods_parse_fragmented_json_responses(
    monkeypatch, method, path, payload
):
    response = FragmentedResponse(
        b'{"accepted":true,"connector_id":"workbuddy-personal","online":true}',
        chunk_size=4,
    )

    class FakeOpener:
        def open(self, _request, timeout):
            assert timeout in (15, 30)
            return response

    monkeypatch.setattr(
        server.urllib.request, "build_opener", lambda *_args: FakeOpener()
    )

    result = getattr(server, method)(path) if method == "_get" else getattr(
        server, method
    )(path, payload)

    assert result["accepted"] is True
    assert response.offset == len(response.body)


def test_read_limited_accepts_exact_limit():
    response = FragmentedResponse(b"exact", chunk_size=1)

    assert server._read_limited(response, 5, "test response") == b"exact"
    assert response.offset == 5


@pytest.mark.parametrize(
    ("method", "path", "payload", "limit"),
    [
        ("_post", "heartbeat", {"status": "idle"}, 256 * 1024),
        ("_get", "presence", None, 64 * 1024),
    ],
)
def test_http_methods_reject_success_bodies_over_configured_limit(
    monkeypatch, method, path, payload, limit
):
    response = FragmentedResponse(b"x" * (limit + 1), chunk_size=16 * 1024)

    class FakeOpener:
        def open(self, _request, timeout):
            assert timeout in (15, 30)
            return response

    monkeypatch.setattr(
        server.urllib.request, "build_opener", lambda *_args: FakeOpener()
    )

    with pytest.raises(RuntimeError, match=f"超过 {limit} 字节限制"):
        if method == "_get":
            server._get(path)
        else:
            server._post(path, payload)

    assert response.offset == limit + 1


@pytest.mark.parametrize("invalid_limit", [True, -1, 3.5, "10", None])
def test_read_limited_rejects_invalid_limit_types_and_values(invalid_limit):
    response = FragmentedResponse(b"body", chunk_size=2)

    with pytest.raises(ValueError, match="non-negative integer"):
        server._read_limited(response, invalid_limit, "test response")

    assert response.offset == 0


class TrackingErrorBody:
    def __init__(self, body: bytes, *, close_error: bool = False):
        self.body = body
        self.offset = 0
        self.closed = False
        self.close_error = close_error
        self.read_sizes = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True
        if self.close_error:
            raise OSError("synthetic close failure")


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("_get", "presence", None),
        ("_post", "heartbeat", {"status": "idle"}),
    ],
)
def test_http_error_response_stream_is_closed_after_reading(
    monkeypatch, method, path, payload
):
    body = TrackingErrorBody(b'{"detail":"rate limited"}')
    http_error = server.urllib.error.HTTPError(
        "https://hub.example.test/" + path, 429, "Too Many Requests", {}, body
    )

    class FakeOpener:
        def open(self, _request, timeout):
            raise http_error

    monkeypatch.setattr(
        server.urllib.request, "build_opener", lambda *_args: FakeOpener()
    )

    with pytest.raises(RuntimeError, match="Hub HTTP 429: rate limited"):
        if method == "_get":
            server._get(path)
        else:
            server._post(path, payload)

    assert body.closed is True
    assert body.offset == len(body.body)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("_get", "presence", None),
        ("_post", "heartbeat", {"status": "idle"}),
    ],
)
def test_oversized_http_error_response_is_bounded_and_closed(
    monkeypatch, method, path, payload
):
    body = TrackingErrorBody(b"x" * (16 * 1024 + 1))
    http_error = server.urllib.error.HTTPError(
        "https://hub.example.test/" + path, 502, "Bad Gateway", {}, body
    )

    class FakeOpener:
        def open(self, _request, timeout):
            raise http_error

    monkeypatch.setattr(
        server.urllib.request, "build_opener", lambda *_args: FakeOpener()
    )

    with pytest.raises(RuntimeError, match="Hub HTTP 502: Hub 返回 HTTP 错误"):
        if method == "_get":
            server._get(path)
        else:
            server._post(path, payload)

    assert body.closed is True
    assert body.offset == 16 * 1024 + 1
    assert all(size <= 16 * 1024 for size in body.read_sizes)


def test_http_error_close_failure_does_not_mask_original_status(monkeypatch):
    body = TrackingErrorBody(b'{"detail":"upstream unavailable"}', close_error=True)
    http_error = server.urllib.error.HTTPError(
        "https://hub.example.test/presence", 503, "Service Unavailable", {}, body
    )

    class FakeOpener:
        def open(self, _request, timeout):
            raise http_error

    monkeypatch.setattr(
        server.urllib.request, "build_opener", lambda *_args: FakeOpener()
    )

    with pytest.raises(RuntimeError, match="Hub HTTP 503: upstream unavailable"):
        server._get("presence")

    assert body.closed is True


# T161–T164: handshake receipt and cache-boundary regression guards.

def test_handshake_rejects_unaccepted_receipt_without_caching(monkeypatch):
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        return {"accepted": False, "connector_id": payload["connector_id"]}

    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(RuntimeError, match="connector identity"):
        server._ensure_handshake()

    assert calls[0][0] == "handshake"
    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None


def test_handshake_rejects_mismatched_connector_identity_without_caching(monkeypatch):
    def fake_post(_path, _payload):
        return {"accepted": True, "connector_id": "another-connector"}

    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(RuntimeError, match="connector identity"):
        server._ensure_handshake()

    assert server.HANDSHAKE_READY is False
    assert server.HANDSHAKE_CACHE_KEY is None


def test_handshake_reuses_receipt_for_unchanged_configuration(monkeypatch):
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        return {"accepted": True, "connector_id": payload["connector_id"]}

    monkeypatch.setattr(server, "_post", fake_post)

    first = server._ensure_handshake()
    second = server._ensure_handshake()

    assert first["accepted"] is True
    assert second == {
        "accepted": True,
        "cached": True,
        "connector_id": "workbuddy-personal",
    }
    assert len(calls) == 1
    assert calls[0][0] == "handshake"


def test_handshake_repeats_after_token_or_hub_url_changes(monkeypatch):
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        return {"accepted": True, "connector_id": payload["connector_id"]}

    monkeypatch.setattr(server, "_post", fake_post)

    server._ensure_handshake()
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN", "y" * 48)
    server._ensure_handshake()
    monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL", "https://hub-two.example.test")
    server._ensure_handshake()

    assert len(calls) == 3
    assert all(path == "handshake" for path, _payload in calls)
    # The raw credential must never be copied into the handshake payload.
    assert all("y" * 48 not in repr(payload) for _path, payload in calls)
