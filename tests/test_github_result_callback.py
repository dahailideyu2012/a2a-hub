from __future__ import annotations

import pytest

from a2a_hub.github_result_callback import (
    GithubResultDeliveryError,
    _marker,
    deliver_github_issue_result,
    render_result_comment,
)


def test_result_comment_escapes_untrusted_html_and_suppresses_mentions():
    body = render_result_comment("task-123", "completed", "<script>alert(1)</script> @everyone")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "@everyone" not in body
    assert "@\u200beveryone" in body
    assert _marker("task-123") in body


def test_result_comment_rejects_nonterminal_state():
    with pytest.raises(GithubResultDeliveryError):
        render_result_comment("task-123", "working", "not done")


def test_callback_fails_closed_for_non_allowlisted_repo():
    with pytest.raises(GithubResultDeliveryError, match="not allowlisted"):
        deliver_github_issue_result(
            {"repository": "attacker/repo", "issue_number": 1},
            "task-123", "completed", "output", "x" * 30, "owner/expected",
        )


def test_callback_rejects_invalid_issue_id_and_credentials():
    with pytest.raises(GithubResultDeliveryError):
        deliver_github_issue_result(
            {"repository": "owner/repo", "issue_number": True},
            "task-123", "completed", "output", "x" * 30, "owner/repo",
        )
    with pytest.raises(GithubResultDeliveryError):
        deliver_github_issue_result(
            {"repository": "owner/repo", "issue_number": 1},
            "task-123", "completed", "output", "short", "owner/repo",
        )
