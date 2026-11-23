from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scripts import live_integration_observer as observer


def _issue():
    return {"state": "open", "labels": [{"name": "a2a-relay-enabled"}]}


def test_default_cli_is_offline_dry_run(capsys):
    with patch.object(observer.urllib.request, "build_opener", side_effect=AssertionError("network must not be used")):
        with patch("sys.argv", ["observer", "--repo", "owner/sandbox", "--issue", "12", "--json"]):
            assert observer.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "DRY_RUN"
    assert report["network_access"] == "none"
    assert report["github_writes"] == 0


def test_observe_requires_explicit_read_only_confirmation(capsys):
    with patch("sys.argv", ["observer", "--repo", "owner/sandbox", "--issue", "12", "--observe"]):
        assert observer.main() == 2
    assert "--confirm-read-only-github" in capsys.readouterr().err


def test_repository_must_match_dedicated_allowlist():
    with pytest.raises(observer.ObserverError, match="allowlist"):
        observer.observe("owner/other", 12, "x" * 32, "owner/sandbox")


def test_short_token_is_rejected_before_network():
    with patch.object(observer, "_request_json", side_effect=AssertionError("network must not be called")):
        with pytest.raises(observer.ObserverError, match="token"):
            observer.observe("owner/sandbox", 12, "short", "owner/sandbox")


def test_issue_must_be_labeled_and_open():
    def fake(url, token):
        if url.endswith("/issues/12"):
            return {"state": "open", "labels": []}
        raise AssertionError("comments must not be fetched before issue validation")

    with patch.object(observer, "_request_json", side_effect=fake):
        with pytest.raises(observer.ObserverError, match="a2a-relay-enabled"):
            observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")


def test_result_marker_confirms_latest_accepted_task():
    task_id = "task_12345678"

    def fake(url, token):
        if url.endswith("/issues/12"):
            return _issue()
        if "/comments?" in url:
            return [
                {"id": 1, "body": "A2A Hub 已接收任务\n\n- Task: `" + task_id + "`"},
                {"id": 2, "body": "<!-- a2a-hub-result:task_12345678 -->\n## A2A Hub 执行结果"},
            ]
        raise AssertionError(url)

    with patch.object(observer, "_request_json", side_effect=fake):
        result = observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")
    assert result["result"] == "ROUNDTRIP_CONFIRMED"
    assert result["task_id"] == task_id
    assert result["github_writes"] == 0


def test_long_task_id_uses_same_truncated_marker_key_as_callback():
    task_id = "task_" + ("a" * 110)

    def fake(url, token):
        if url.endswith("/issues/12"):
            return _issue()
        if "/comments?" in url:
            return [
                {"id": 1, "body": "A2A Hub 已接收任务\n\n- Task: `" + task_id + "`"},
                {"id": 2, "body": "<!-- a2a-hub-result:" + ("task_" + "a" * 95) + " -->"},
            ]
        raise AssertionError(url)

    with patch.object(observer, "_request_json", side_effect=fake):
        result = observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")
    assert result["result"] == "ROUNDTRIP_CONFIRMED"


def test_missing_result_marker_waits_for_workbuddy():
    def fake(url, token):
        if url.endswith("/issues/12"):
            return _issue()
        return [{"id": 1, "body": "A2A Hub 已接收任务\n\n- Task: `task_12345678`"}]

    with patch.object(observer, "_request_json", side_effect=fake):
        result = observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")
    assert result["result"] == "WAITING_FOR_WORKBUDDY"


def test_rejects_pull_request_and_closed_issue():
    with patch.object(observer, "_request_json", return_value={"pull_request": {}, "state": "open", "labels": []}):
        with pytest.raises(observer.ObserverError, match="not a pull request"):
            observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")
    with patch.object(observer, "_request_json", return_value={"state": "closed", "labels": []}):
        with pytest.raises(observer.ObserverError, match="remain open"):
            observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")


def test_response_size_limit_is_enforced():
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"x" * (1024 * 1024 + 1)
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(observer.urllib.request, "build_opener", return_value=opener):
        with pytest.raises(observer.ObserverError, match="1 MiB"):
            observer._request_json("https://api.github.com/test", "x" * 32)
    response.read.assert_called_once_with(1024 * 1024 + 1)


def test_redirect_handler_refuses_redirect():
    handler = observer._NO_REDIRECT()
    request = observer.urllib.request.Request("https://api.github.com/test")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example") is None


def test_http_error_does_not_expose_response_body_or_token():
    token = "secret-token-value"
    error = urllib.error.HTTPError(
        "https://api.github.com/test", 403, "Forbidden", {}, io.BytesIO(b"private-response-body")
    )
    opener = MagicMock()
    opener.open.side_effect = error
    with patch.object(observer.urllib.request, "build_opener", return_value=opener):
        with pytest.raises(observer.ObserverError) as caught:
            observer._request_json("https://api.github.com/test", token)
    message = str(caught.value)
    assert "403" in message
    assert token not in message
    assert "private-response-body" not in message


def test_comment_pagination_stops_after_1000_comments():
    calls = []

    def fake(url, token):
        calls.append(url)
        if url.endswith("/issues/12"):
            return _issue()
        return [{"id": i, "body": "ordinary comment"} for i in range(100)]

    with patch.object(observer, "_request_json", side_effect=fake):
        with pytest.raises(observer.ObserverError, match="1,000-comment"):
            observer.observe("owner/sandbox", 12, "x" * 32, "owner/sandbox")
    assert len(calls) == 11
