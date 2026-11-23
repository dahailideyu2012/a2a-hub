from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from scripts import workbuddy_real_hub_ui_acceptance as ui


def test_http_json_refuses_non_loopback_before_network():
    with patch.object(urllib.request, "build_opener", side_effect=AssertionError("network must not be used")):
        with pytest.raises(RuntimeError, match="loopback HTTP only"):
            ui.http_json("https://example.com/api")


def test_http_json_rejects_redirects():
    handler = ui._NoRedirect()
    request = urllib.request.Request("http://127.0.0.1/test")
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/other") is None


def test_http_json_redacts_error_response_body_and_token():
    secret = "synthetic-secret-token"
    error = urllib.error.HTTPError(
        "http://127.0.0.1/test", 403, "Forbidden", {}, io.BytesIO(b"private-upstream-body")
    )
    opener = MagicMock()
    opener.open.side_effect = error
    with patch.object(urllib.request, "build_opener", return_value=opener):
        with pytest.raises(RuntimeError) as caught:
            ui.http_json("http://127.0.0.1/test", headers={"Authorization": secret})
    message = str(caught.value)
    assert "HTTP 403" in message
    assert secret not in message
    assert "private-upstream-body" not in message


def test_http_json_enforces_one_mib_response_limit():
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b"x" * (1024 * 1024 + 1)
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(urllib.request, "build_opener", return_value=opener):
        with pytest.raises(RuntimeError, match="1 MiB"):
            ui.http_json("http://127.0.0.1/test")
    response.read.assert_called_once_with(1024 * 1024 + 1)


def test_task_text_extracts_only_text_parts():
    task = {"history": [
        {"parts": [{"text": "first"}, {"data": {"secret": "not text"}}]},
        {"parts": [{"text": "second"}]},
    ]}
    assert ui.task_text(task) == "first" + chr(10) + "second"


@pytest.mark.parametrize(("path", "expected"), [
    ("/integrations/workbuddy/v1/claim", "claim"),
    ("/integrations/workbuddy/v1/tasks/abc/start", "start"),
    ("/integrations/workbuddy/v1/tasks/abc/progress", "progress"),
    ("/integrations/workbuddy/v1/tasks/abc/renew", "renew"),
    ("/integrations/workbuddy/v1/tasks/abc/result", "result"),
    ("/admin/secrets", "unknown"),
])
def test_proxy_action_classifies_only_connector_routes(path, expected):
    assert ui.proxy_action(path) == expected


def test_proxy_replaces_user_connector_token_with_synthetic_token():
    captured = {}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured["token"] = self.headers.get("X-WorkBuddy-Connector-Token")
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    proxy = ui.LoopbackProxy(("127.0.0.1", 0), ui.WorkBuddyProxyHandler)
    proxy.state = ui.ProxyState(f"http://127.0.0.1:{upstream.server_port}")
    threads = [
        threading.Thread(target=upstream.serve_forever, daemon=True),
        threading.Thread(target=proxy.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/integrations/workbuddy/v1/claim",
            data=b"{}",
            headers={"Content-Type": "application/json",
                     "X-WorkBuddy-Connector-Token": "real-user-token-must-not-forward"},
            method="POST",
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"ok": True}
        assert captured["token"] == ui.WORKBUDDY_TOKEN
        assert captured["token"] != "real-user-token-must-not-forward"
        assert proxy.state.events[0]["action"] == "claim"
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_copy_isolated_app_does_not_copy_env_or_runtime_data(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "a2a_hub").mkdir(parents=True)
    (source / "a2a_hub" / "server.py").write_text("safe", encoding="utf-8")
    (source / "web").mkdir()
    (source / "web" / "index.html").write_text("ui", encoding="utf-8")
    (source / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "prod.sqlite3").write_text("do-not-copy", encoding="utf-8")
    monkeypatch.setattr(ui, "ROOT", source)
    target = ui.copy_isolated_app(tmp_path / "isolated")
    assert (target / "a2a_hub" / "server.py").read_text(encoding="utf-8") == "safe"
    assert (target / "web" / "index.html").exists()
    assert not (target / ".env").exists()
    assert not (target / "data").exists()


def test_stop_process_none_is_already_stopped():
    assert ui.stop_process(None) is True


def _proxy_roundtrip(upstream_status: int, upstream_body: bytes) -> tuple[int, bytes]:
    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(upstream_status)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.end_headers()
            self.wfile.write(upstream_body)

        def log_message(self, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    proxy = ui.LoopbackProxy(("127.0.0.1", 0), ui.WorkBuddyProxyHandler)
    proxy.state = ui.ProxyState(f"http://127.0.0.1:{upstream.server_port}")
    threads = [
        threading.Thread(target=upstream.serve_forever, daemon=True),
        threading.Thread(target=proxy.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/integrations/workbuddy/v1/claim",
            data=b"{}",
            headers={"X-WorkBuddy-Connector-Token": "synthetic-client-placeholder"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code if isinstance(response, urllib.error.HTTPError) else response.status, response.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_proxy_rejects_upstream_response_over_256_kib():
    status, body = _proxy_roundtrip(200, b"x" * (256 * 1024 + 1))
    assert status == 502
    assert json.loads(body) == {"detail": "isolated Hub response exceeded the proxy limit"}


def test_proxy_redacts_upstream_http_error_body():
    private_body = b"private-upstream-stacktrace-and-secret"
    status, body = _proxy_roundtrip(500, private_body)
    assert status == 500
    assert private_body not in body
    assert json.loads(body) == {"detail": "isolated Hub returned HTTP 500"}


def test_report_readback_never_contains_task_history_or_prompt():
    task_history = "sensitive full prompt and private conversation"
    result = ui.build_readback("completed", task_history + " " + ui.TASK_MARKER + " " + ui.EXPECTED_CALCULATION)
    serialized = json.dumps(result)
    assert result == {
        "task_state": "completed",
        "marker_present": True,
        "calculation_present": True,
    }
    assert task_history not in serialized
    assert "history" not in serialized
    assert "prompt" not in serialized


def test_task_text_handles_empty_history():
    assert ui.task_text({"history": []}) == ""
    assert ui.task_text({}) == ""


@pytest.mark.parametrize(
    ("hub_stopped", "temporary_directory_cleaned", "expected"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
        (False, False, False),
    ],
)
def test_cleanup_failure_prevents_pass(hub_stopped, temporary_directory_cleaned, expected):
    report = {"result": "PASS"}
    passed = ui.apply_cleanup_verdict(
        report,
        hub_stopped=hub_stopped,
        temporary_directory_cleaned=temporary_directory_cleaned,
    )
    assert passed is expected
    assert report["hub_process_exited"] is hub_stopped
    if not expected:
        assert report["result"] == "FAIL"
