"""Full GitHub Issue -> Hub -> WorkBuddy -> GitHub callback integration tests.

These tests exercise the production ingress and connector routes with a local
GitHub API stub. No network requests or real credentials are used.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error

import pytest

from a2a_hub.store import SqliteTaskStore


class StubResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


class GitHubApiStub:
    """Minimal in-memory Issue comments API with ambiguous POST support."""

    def __init__(self):
        self.comments = []
        self.get_count = 0
        self.post_count = 0
        self.lose_first_post_response = True

    def open(self, request, timeout):
        assert timeout == 12
        assert request.full_url.startswith(
            "https://api.github.com/repos/owner/repo/issues/42/comments?"
        ) or request.full_url == (
            "https://api.github.com/repos/owner/repo/issues/42/comments"
        )
        assert request.get_header("Authorization") == "Bearer " + "S" * 32
        if request.get_method() == "GET":
            self.get_count += 1
            return StubResponse(self.comments)
        assert request.get_method() == "POST"
        self.post_count += 1
        payload = json.loads(request.data.decode("utf-8"))
        self.comments.append({"body": payload["body"], "html_url": "https://github.test/comment/1"})
        if self.lose_first_post_response:
            self.lose_first_post_response = False
            raise urllib.error.URLError("simulated response loss after GitHub commit")
        return StubResponse({"html_url": "https://github.test/comment/1"})


@pytest.mark.asyncio
async def test_signed_issue_to_workbuddy_to_github_roundtrip_and_ambiguous_retry(
    client, tmp_path, monkeypatch
):
    import a2a_hub.github_result_callback as callback_module
    import a2a_hub.server as server

    # Replace the test app's default in-memory store with a disposable SQLite DB.
    store = SqliteTaskStore(str(tmp_path / "t5-roundtrip.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)

    secret = "H" * 40
    api_token = "S" * 32
    server.hub.settings.github_relay_hmac_secret = secret
    server.hub.settings.github_relay_api_token = api_token
    server.hub.settings.github_relay_allowed_repos = "owner/repo"
    server.hub.settings.workbuddy_connector_allowed_agents = "echo-a"

    github = GitHubApiStub()
    monkeypatch.setattr(
        callback_module.urllib.request,
        "build_opener",
        lambda *_args, **_kwargs: github,
    )

    payload = {
        "agent_id": "echo-a",
        "prompt": "Run the requested task",
        "mode": "pull",
        "github_issue_callback": {
            "repository": "owner/repo",
            "issue_number": 42,
            "source_comment_id": 9001,
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    key = "gh-1234-42-9001"
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + key.encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    ingress_headers = {
        "X-ChatGPT-Ingress-Token": "test-chatgpt-ingress-token-0123456789",
        "Idempotency-Key": key,
        "X-A2A-Relay-Source": "github-issue-comment",
        "X-A2A-Relay-Timestamp": timestamp,
        "X-A2A-Relay-Signature": signature,
        "Content-Type": "application/json",
    }

    submitted = client.post(
        "/integrations/chatgpt/v1/tasks", content=raw, headers=ingress_headers
    )
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task"]["id"]
    assert submitted.json()["task"]["status"]["state"] == "submitted"
    assert "github_issue_callback" not in json.dumps(submitted.json())

    connector_headers = {
        "X-WorkBuddy-Connector-Token": "test-workbuddy-connector-token-0123456789"
    }
    claimed = client.post(
        "/integrations/workbuddy/v1/claim",
        headers=connector_headers,
        json={"agent_ids": ["echo-a"]},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["claimed"] is True
    assert claimed.json()["task"]["id"] == task_id
    assert "github_issue_callback" not in json.dumps(claimed.json())

    lease = claimed.json()["lease"]
    lease_body = {
        "lease_token": lease["lease_token"],
        "lease_version": lease["lease_version"],
    }
    started = client.post(
        f"/integrations/workbuddy/v1/tasks/{task_id}/start",
        headers=connector_headers,
        json=lease_body,
    )
    assert started.status_code == 200, started.text

    result_body = {
        **lease_body,
        "result_key": "t5-stable-result-key-001",
        "state": "completed",
        "output": "WorkBuddy completed the task",
    }
    first = client.post(
        f"/integrations/workbuddy/v1/tasks/{task_id}/result",
        headers=connector_headers,
        json=result_body,
    )
    # GitHub accepted the comment, but the simulated response was lost.
    assert first.status_code == 502
    assert len(github.comments) == 1
    assert github.post_count == 1
    assert store.get(task_id).status.state.value == "completed"

    # WorkBuddy retries the exact same result key/body. The GET finds the
    # deterministic marker and the callback does not POST a duplicate.
    retry = client.post(
        f"/integrations/workbuddy/v1/tasks/{task_id}/result",
        headers=connector_headers,
        json=result_body,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["idempotent_replay"] is True
    assert retry.json()["github_callback"]["replayed"] is True
    assert github.get_count == 2
    assert github.post_count == 1
    assert len(github.comments) == 1
    assert "WorkBuddy completed the task" in github.comments[0]["body"]

    # The task remains readable after callback recovery.
    readback = client.get(
        f"/integrations/chatgpt/v1/tasks/{task_id}",
        headers={"X-ChatGPT-Ingress-Token": "test-chatgpt-ingress-token-0123456789"},
    )
    assert readback.status_code == 200
    assert readback.json()["status"]["state"] == "completed"
    store.close()
