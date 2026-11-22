from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts import live_integration_observer as observer


def test_default_cli_is_offline_dry_run(capsys):
    with patch.object(observer.urllib.request, "build_opener", side_effect=AssertionError("network must not be used")):
        assert observer.main_args if hasattr(observer, "main_args") else True
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
            return {"state": "open", "labels": [{"name": "a2a-relay-enabled"}]}
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


def test_missing_result_marker_waits_for_workbuddy():
    def fake(url, token):
        if url.endswith("/issues/12"):
            return {"state": "open", "labels": [{"name": "a2a-relay-enabled"}]}
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
