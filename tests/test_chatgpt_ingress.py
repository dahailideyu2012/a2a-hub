"""Dedicated ChatGPT ingress security and task lifecycle tests."""
from __future__ import annotations

import json

import pytest


TOKEN = "test-chatgpt-ingress-token-0123456789"
BASE = "/integrations/chatgpt/v1"


def headers(key: str = "request-0001") -> dict[str, str]:
    return {
        "X-ChatGPT-Ingress-Token": TOKEN,
        "Idempotency-Key": key,
    }


def payload(prompt: str = "hello") -> dict[str, str]:
    return {"agent_id": "echo-a", "prompt": prompt}


class TestChatGPTIngress:
    def test_missing_token_is_rejected(self, client):
        response = client.get(f"{BASE}/tasks")
        assert response.status_code == 401

    def test_invalid_token_is_rejected(self, client):
        response = client.get(
            f"{BASE}/tasks",
            headers={"X-ChatGPT-Ingress-Token": "wrong-token"},
        )
        assert response.status_code == 403

    def test_submit_requires_idempotency_key(self, client):
        response = client.post(
            f"{BASE}/tasks",
            headers={"X-ChatGPT-Ingress-Token": TOKEN},
            json=payload(),
        )
        assert response.status_code == 422

    def test_submit_and_read_task(self, client):
        response = client.post(f"{BASE}/tasks", headers=headers(), json=payload())
        assert response.status_code == 200
        body = response.json()
        task_id = body["task"]["id"]
        assert body["idempotent_replay"] is False
        assert "chatgpt_ingress_operation_id" not in body["task"]["metadata"]
        assert "chatgpt_ingress_request_hash" not in body["task"]["metadata"]
        assert client.get(f"{BASE}/tasks/{task_id}", headers=headers()).status_code == 200

    def test_same_idempotency_key_replays_same_task(self, client):
        first = client.post(f"{BASE}/tasks", headers=headers("replay-0001"), json=payload())
        second = client.post(f"{BASE}/tasks", headers=headers("replay-0001"), json=payload())
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["task"]["id"] == second.json()["task"]["id"]
        assert second.json()["idempotent_replay"] is True

    def test_complete_failure_retry_recovers_same_task(self, client, monkeypatch):
        from fastapi import HTTPException
        from a2a_hub.chatgpt_ingress import IngressLedger
        import a2a_hub.server as server

        original_complete = IngressLedger.complete
        calls = 0

        async def fail_once(self, key, request_hash, task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPException(status_code=502, detail="injected ledger completion failure")
            await original_complete(self, key, request_hash, task_id)

        monkeypatch.setattr(IngressLedger, "complete", fail_once)
        key = "complete-failure-01"
        first = client.post(f"{BASE}/tasks", headers=headers(key), json=payload())
        assert first.status_code == 502
        second = client.post(f"{BASE}/tasks", headers=headers(key), json=payload())
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True
        operation_id = __import__("hashlib").sha256(key.encode()).hexdigest()
        tasks = server.hub.registry.store.list(100)
        matching = [task for task in tasks if task.metadata.get("chatgpt_ingress_operation_id") == operation_id]
        assert len(matching) == 1
        assert second.json()["task"]["id"] == matching[0].id

    def test_complete_failure_is_reconciled_by_task_list(self, client, monkeypatch):
        from fastapi import HTTPException
        from a2a_hub.chatgpt_ingress import IngressLedger
        import a2a_hub.server as server

        original_complete = IngressLedger.complete
        calls = 0

        async def fail_once(self, key, request_hash, task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPException(status_code=502, detail="injected ledger completion failure")
            await original_complete(self, key, request_hash, task_id)

        monkeypatch.setattr(IngressLedger, "complete", fail_once)
        response = client.post(
            f"{BASE}/tasks", headers=headers("list-reconcile-001"), json=payload()
        )
        assert response.status_code == 502
        listed = client.get(
            f"{BASE}/tasks", headers={"X-ChatGPT-Ingress-Token": TOKEN}
        )
        assert listed.status_code == 200
        assert len(listed.json()["tasks"]) == 1
        assert calls == 2
        assert len(server.hub.registry.store.list(100)) == 1

    def test_postwrite_read_failure_retry_replays_same_task(self, client, monkeypatch):
        import a2a_hub.server as server

        store = server.hub.registry.store
        original_get = store.get
        calls = 0

        def fail_first_read(task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_get(task_id)

        monkeypatch.setattr(store, "get", fail_first_read)
        key = "postread-failure-01"
        first = client.post(f"{BASE}/tasks", headers=headers(key), json=payload())
        assert first.status_code == 502
        second = client.post(f"{BASE}/tasks", headers=headers(key), json=payload())
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True
        assert len(store.list(100)) == 1

    def test_reused_key_with_different_payload_conflicts(self, client):
        key = "conflict-001"
        first = client.post(f"{BASE}/tasks", headers=headers(key), json=payload("first"))
        second = client.post(f"{BASE}/tasks", headers=headers(key), json=payload("different"))
        assert first.status_code == 200
        assert second.status_code == 409

    def test_agent_outside_allowlist_is_rejected(self, client):
        response = client.post(
            f"{BASE}/tasks",
            headers=headers("blocked-001"),
            json={"agent_id": "echo-b", "prompt": "hello"},
        )
        assert response.status_code == 403

    def test_extra_fields_are_rejected(self, client):
        response = client.post(
            f"{BASE}/tasks",
            headers=headers("extra-0001"),
            json={**payload(), "method": "social/approve"},
        )
        assert response.status_code == 422

    def test_task_list_contains_only_ingress_tasks(self, client):
        response = client.post(f"{BASE}/tasks", headers=headers("list-00001"), json=payload())
        task_id = response.json()["task"]["id"]
        listed = client.get(f"{BASE}/tasks", headers={"X-ChatGPT-Ingress-Token": TOKEN})
        assert listed.status_code == 200
        assert task_id in {task["id"] for task in listed.json()["tasks"]}

    def test_task_events_returns_snapshot_and_done_for_terminal_task(self, client):
        response = client.post(f"{BASE}/tasks", headers=headers("events-0001"), json=payload())
        task_id = response.json()["task"]["id"]
        events = client.get(f"{BASE}/tasks/{task_id}/events", headers={"X-ChatGPT-Ingress-Token": TOKEN})
        assert events.status_code == 200
        assert "event: task" in events.text
        assert "event: done" in events.text

    def test_body_size_limit(self, client):
        response = client.post(
            f"{BASE}/tasks",
            headers={**headers("large-0001"), "Content-Type": "application/json"},
            content=json.dumps({"agent_id": "echo-a", "prompt": "x" * 20000}),
        )
        assert response.status_code == 413

    def test_non_json_content_type_rejected(self, client):
        response = client.post(
            f"{BASE}/tasks",
            headers={**headers("type-00001"), "Content-Type": "text/plain"},
            content="hello",
        )
        assert response.status_code == 415

    def test_unconfigured_or_short_token_fails_closed(self, client):
        import a2a_hub.server as server

        original = server.hub.settings.chatgpt_ingress_token
        try:
            server.hub.settings.chatgpt_ingress_token = "too-short"
            response = client.get(
                f"{BASE}/tasks",
                headers={"X-ChatGPT-Ingress-Token": "too-short"},
            )
            assert response.status_code == 503
        finally:
            server.hub.settings.chatgpt_ingress_token = original

    def test_rate_limit_returns_429(self, client):
        import a2a_hub.server as server

        original = server.hub.settings.chatgpt_ingress_rate_limit
        try:
            server.hub.settings.chatgpt_ingress_rate_limit = 1
            first = client.get(
                f"{BASE}/tasks",
                headers={"X-ChatGPT-Ingress-Token": TOKEN},
            )
            second = client.get(
                f"{BASE}/tasks",
                headers={"X-ChatGPT-Ingress-Token": TOKEN},
            )
            assert first.status_code == 200
            assert second.status_code == 429
        finally:
            server.hub.settings.chatgpt_ingress_rate_limit = original

    def test_complete_restores_reservation_after_expiry_cleanup(self, tmp_path):
        import asyncio
        import sqlite3
        from a2a_hub.chatgpt_ingress import IdempotencyConflict, IngressLedger

        ledger = IngressLedger(str(tmp_path / "ingress-ledger.db"))
        key = "expired-reservation-01"
        request_hash = "a" * 64

        async def scenario():
            await ledger.reserve(key, request_hash)
            conn = sqlite3.connect(str(ledger.path))
            try:
                conn.execute(
                    "UPDATE chatgpt_ingress_requests SET expires_at=0 WHERE idempotency_key=?",
                    (key,),
                )
                conn.commit()
            finally:
                conn.close()

            # A later reservation performs the normal expired-row cleanup.
            await ledger.reserve("cleanup-trigger-0001", "b" * 64)
            await ledger.complete(key, request_hash, "task-recovered-01")
            assert "task-recovered-01" in await ledger.task_ids(10)

            # Completion is idempotent for the same task, but cannot rebind it.
            await ledger.complete(key, request_hash, "task-recovered-01")
            with pytest.raises(IdempotencyConflict):
                await ledger.complete(key, request_hash, "different-task-01")
            with pytest.raises(IdempotencyConflict):
                await ledger.complete(key, "c" * 64, "task-recovered-01")

        asyncio.run(scenario())

    def test_expired_key_conflict_releases_new_reservation(self, client):
        import sqlite3
        import a2a_hub.server as server

        key = "expired-key-conflict-01"
        first = client.post(f"{BASE}/tasks", headers=headers(key), json=payload("original"))
        assert first.status_code == 200

        ledger_path = str(server.hub.settings.chatgpt_ingress_db_path_resolved())
        conn = sqlite3.connect(ledger_path)
        try:
            conn.execute(
                "UPDATE chatgpt_ingress_requests SET expires_at=0 WHERE idempotency_key=?",
                (key,),
            )
            conn.commit()
        finally:
            conn.close()

        conflicting = client.post(
            f"{BASE}/tasks", headers=headers(key), json=payload("different")
        )
        assert conflicting.status_code == 409

        # The rejected payload must not leave a pending reservation that blocks
        # the original payload from replaying the already accepted task.
        replay = client.post(
            f"{BASE}/tasks", headers=headers(key), json=payload("original")
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["task"]["id"] == first.json()["task"]["id"]


def test_github_relay_valid_hmac_is_accepted(client, monkeypatch, tmp_path):
    import hashlib
    import hmac
    import json
    import time
    import a2a_hub.server as server
    from a2a_hub.store import SqliteTaskStore

    relay_store = SqliteTaskStore(str(tmp_path / "relay-pull.db"))
    monkeypatch.setattr(server.hub.registry, "store", relay_store)
    secret = "relay-test-secret-0123456789-abcdef"
    monkeypatch.setattr(server.hub.settings, "github_relay_hmac_secret", secret)
    key = "gh-1234-5-5678"
    server.hub.settings.github_relay_api_token = "github-test-token-0123456789"
    server.hub.settings.github_relay_allowed_repos = "owner/repo"
    raw = json.dumps({
        "agent_id": "echo-a", "prompt": "signed relay", "mode": "pull",
        "github_issue_callback": {
            "repository": "owner/repo", "issue_number": 5, "source_comment_id": 5678
        },
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    message = timestamp.encode() + b"." + key.encode() + b"." + raw
    signature = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    response = client.post(
        f"{BASE}/tasks",
        headers={
            **headers(key),
            "X-A2A-Relay-Source": "github-issue-comment",
            "X-A2A-Relay-Timestamp": timestamp,
            "X-A2A-Relay-Signature": signature,
            "Content-Type": "application/json",
        },
        content=raw,
    )
    assert response.status_code == 200
    assert response.json()["task"]["id"]
    assert "github_issue_callback" not in response.json()["task"]["metadata"]
    relay_store.close()


def test_github_relay_rejects_stale_hmac(client, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    import a2a_hub.server as server

    secret = "relay-test-secret-0123456789-abcdef"
    monkeypatch.setattr(server.hub.settings, "github_relay_hmac_secret", secret)
    key = "gh-1234-5-5679"
    raw = json.dumps({"agent_id": "echo-a", "prompt": "stale relay", "mode": "pull"},
                     ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()) - 600)
    message = timestamp.encode() + b"." + key.encode() + b"." + raw
    signature = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    response = client.post(
        f"{BASE}/tasks",
        headers={
            **headers(key),
            "X-A2A-Relay-Source": "github-issue-comment",
            "X-A2A-Relay-Timestamp": timestamp,
            "X-A2A-Relay-Signature": signature,
            "Content-Type": "application/json",
        },
        content=raw,
    )
    assert response.status_code == 401


def test_github_relay_rejects_bad_signature(client, monkeypatch):
    import json
    import time
    import a2a_hub.server as server

    monkeypatch.setattr(server.hub.settings, "github_relay_hmac_secret",
                        "relay-test-secret-0123456789-abcdef")
    key = "gh-1234-5-5680"
    raw = json.dumps({"agent_id": "echo-a", "prompt": "tampered", "mode": "pull"},
                     ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    response = client.post(
        f"{BASE}/tasks",
        headers={
            **headers(key),
            "X-A2A-Relay-Source": "github-issue-comment",
            "X-A2A-Relay-Timestamp": str(int(time.time())),
            "X-A2A-Relay-Signature": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
        content=raw,
    )
    assert response.status_code == 403



def test_unsigned_request_cannot_supply_github_callback_context(client, tmp_path, monkeypatch):
    response = client.post(
        f"{BASE}/tasks",
        headers=headers("unsigned-callback-01"),
        json={
            "agent_id": "echo-a",
            "prompt": "must not be able to choose an Issue callback",
            "mode": "pull",
            "github_issue_callback": {
                "repository": "owner/repo",
                "issue_number": 5,
                "source_comment_id": 123,
            },
        },
    )
    assert response.status_code == 403


def test_chatgpt_ingress_budget_is_shared_between_independent_routers(client, monkeypatch, tmp_path):
    """Separate ASGI apps (representing workers) must consume one shared budget."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import a2a_hub.server as server
    from a2a_hub.chatgpt_ingress import build_chatgpt_ingress_router

    settings = server.hub.settings
    monkeypatch.setattr(settings, "chatgpt_ingress_rate_limit", 1)
    monkeypatch.setattr(
        settings,
        "chatgpt_ingress_rate_limit_db_path",
        str(tmp_path / "shared-ingress-limits.db"),
    )
    token_headers = {"X-ChatGPT-Ingress-Token": TOKEN}

    first = client.get(f"{BASE}/tasks", headers=token_headers)
    assert first.status_code == 200

    second_app = FastAPI()
    second_app.include_router(build_chatgpt_ingress_router(lambda: server.hub))
    with TestClient(second_app) as second_client:
        second = second_client.get(f"{BASE}/tasks", headers=token_headers)

    assert second.status_code == 429


def test_chatgpt_ingress_limiter_storage_failure_fails_closed(client, monkeypatch, tmp_path):
    import a2a_hub.server as server

    broken_path = tmp_path / "not-a-database-directory"
    broken_path.mkdir()
    monkeypatch.setattr(
        server.hub.settings,
        "chatgpt_ingress_rate_limit_db_path",
        str(broken_path),
    )
    response = client.get(
        f"{BASE}/tasks",
        headers={"X-ChatGPT-Ingress-Token": TOKEN},
    )
    assert response.status_code == 503
    assert "限流存储暂不可用" in response.json()["detail"]
    assert "not-a-database-directory" not in response.text


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", f"{BASE}/tasks", {}),
        ("POST", f"{BASE}/tasks", {"headers": {"Idempotency-Key": "route-guard-0001"}, "json": payload()}),
        ("GET", f"{BASE}/tasks/task-missing", {}),
        ("POST", f"{BASE}/tasks/task-missing/cancel", {}),
        ("GET", f"{BASE}/tasks/task-missing/events", {}),
    ],
)
def test_every_ingress_route_consumes_shared_rate_limit(
    client, monkeypatch, tmp_path, method, path, kwargs
):
    import a2a_hub.server as server

    monkeypatch.setattr(server.hub.settings, "chatgpt_ingress_rate_limit", 1)
    monkeypatch.setattr(
        server.hub.settings,
        "chatgpt_ingress_rate_limit_db_path",
        str(tmp_path / "all-ingress-routes.db"),
    )
    first = client.get(f"{BASE}/tasks", headers={"X-ChatGPT-Ingress-Token": TOKEN})
    assert first.status_code == 200

    request_kwargs = dict(kwargs)
    request_headers = dict(request_kwargs.pop("headers", {}))
    request_headers["X-ChatGPT-Ingress-Token"] = TOKEN
    response = client.request(method, path, headers=request_headers, **request_kwargs)
    assert response.status_code == 429


def test_changing_ingress_budget_does_not_reset_current_window(client, monkeypatch, tmp_path):
    import a2a_hub.server as server

    monkeypatch.setattr(server.hub.settings, "chatgpt_ingress_rate_limit", 2)
    monkeypatch.setattr(
        server.hub.settings,
        "chatgpt_ingress_rate_limit_db_path",
        str(tmp_path / "stable-ingress-bucket.db"),
    )
    first = client.get(f"{BASE}/tasks", headers={"X-ChatGPT-Ingress-Token": TOKEN})
    assert first.status_code == 200

    # Tightening the configured budget during an active window must use the
    # same token bucket rather than creating a new, empty bucket.
    monkeypatch.setattr(server.hub.settings, "chatgpt_ingress_rate_limit", 1)
    second = client.get(f"{BASE}/tasks", headers={"X-ChatGPT-Ingress-Token": TOKEN})
    assert second.status_code == 429
