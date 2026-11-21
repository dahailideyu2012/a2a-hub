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
