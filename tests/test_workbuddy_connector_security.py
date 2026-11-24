"""WorkBuddy connector security controls (T31)."""
from __future__ import annotations

import pytest


def _configure_connector(monkeypatch, tmp_path, *, suffix: str, limit: int):
    import a2a_hub.server as server
    from a2a_hub.store import SqliteTaskStore

    settings = server.hub.settings
    token = f"test-workbuddy-rate-limit-{suffix}-0123456789"
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", f"workbuddy-{suffix}")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", limit)
    monkeypatch.setattr(settings, "workbuddy_rate_limit_db_path", str(tmp_path / f"{suffix}-rate.db"))
    store = SqliteTaskStore(str(tmp_path / f"{suffix}.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)
    return settings, store, token


def test_connector_rate_limit_runs_before_task_store_claim(client, monkeypatch, tmp_path):
    settings, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix="rate-order", limit=1
    )
    calls = 0
    original_claim = store.claim_next

    def counted_claim(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_next", counted_claim)
    headers = {"X-WorkBuddy-Connector-Token": token}
    url = "/integrations/workbuddy/v1/claim"

    first = client.post(url, headers=headers, json={})
    second = client.post(url, headers=headers, json={})
    assert first.status_code == 200
    assert first.json() == {"claimed": False}
    assert second.status_code == 429
    assert calls == 1

    # Changing the numeric policy reuses the same bucket: increasing the limit
    # grants only the additional capacity, and lowering it applies immediately.
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 2)
    third = client.post(url, headers=headers, json={})
    assert third.status_code == 200
    assert calls == 2
    fourth = client.post(url, headers=headers, json={})
    assert fourth.status_code == 429
    assert calls == 2
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 1)
    fifth = client.post(url, headers=headers, json={})
    assert fifth.status_code == 429
    assert calls == 2
    store.close()


def test_missing_and_invalid_tokens_do_not_consume_connector_budget(
    client, monkeypatch, tmp_path
):
    _, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix="auth-order", limit=1
    )
    url = "/integrations/workbuddy/v1/claim"

    missing = client.post(url, json={})
    invalid = client.post(
        url,
        headers={"X-WorkBuddy-Connector-Token": "wrong-token"},
        json={},
    )
    valid = client.post(
        url,
        headers={"X-WorkBuddy-Connector-Token": token},
        json={},
    )
    limited = client.post(
        url,
        headers={"X-WorkBuddy-Connector-Token": token},
        json={},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert valid.status_code == 200
    assert limited.status_code == 429
    store.close()


def test_connector_audit_is_single_line_allowlisted_and_redacted(
    client, monkeypatch, tmp_path, caplog
):
    import hashlib
    import json
    import logging
    from a2a_hub.models import Message, Task

    _, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix="audit-redaction", limit=60
    )
    prompt_secret = "PRIVATE-PROMPT-DO-NOT-LOG"
    output_secret = "PRIVATE-OUTPUT-DO-NOT-LOG"
    task = Task(agentId="echo-a")
    task.history.append(Message.user(prompt_secret))
    store.enqueue_pull(task)

    caplog.set_level(logging.INFO, logger="a2a_hub.audit.workbuddy")
    headers = {"X-WorkBuddy-Connector-Token": token}
    claimed = client.post("/integrations/workbuddy/v1/claim", headers=headers, json={})
    assert claimed.status_code == 200
    lease = claimed.json()["lease"]
    lease_token = lease["lease_token"]
    lease_body = {
        "lease_token": lease_token,
        "lease_version": lease["lease_version"],
    }
    started = client.post(
        f"/integrations/workbuddy/v1/tasks/{task.id}/start",
        headers=headers,
        json=lease_body,
    )
    assert started.status_code == 200
    finished = client.post(
        f"/integrations/workbuddy/v1/tasks/{task.id}/result",
        headers=headers,
        json={
            **lease_body,
            "result_key": "audit-result-0001",
            "state": "completed",
            "output": output_secret,
        },
    )
    assert finished.status_code == 200

    records = [
        record for record in caplog.records
        if record.name == "a2a_hub.audit.workbuddy"
    ]
    assert [json.loads(record.message)["event"] for record in records] == [
        "claim", "start", "result"
    ]
    expected_keys = {
        "event", "outcome", "connector_id", "status", "token_fp", "duration_ms"
    }
    for record in records:
        assert "\n" not in record.message
        payload = json.loads(record.message)
        assert set(payload) == expected_keys
        assert payload["outcome"] == "ok"
        assert payload["token_fp"] == hashlib.sha256(token.encode()).hexdigest()[:8]
        for secret in (token, lease_token, prompt_secret, output_secret):
            assert secret not in record.message
    store.close()


def test_connector_audit_records_auth_and_rate_limit_outcomes(
    client, monkeypatch, tmp_path, caplog
):
    import hashlib
    import json
    import logging

    _, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix="audit-denials", limit=1
    )
    caplog.set_level(logging.INFO, logger="a2a_hub.audit.workbuddy")
    url = "/integrations/workbuddy/v1/claim"

    assert client.post(url, json={}).status_code == 401
    wrong = "wrong-workbuddy-token-0123456789"
    assert client.post(
        url, headers={"X-WorkBuddy-Connector-Token": wrong}, json={}
    ).status_code == 403
    headers = {"X-WorkBuddy-Connector-Token": token}
    assert client.post(url, headers=headers, json={}).status_code == 200
    assert client.post(url, headers=headers, json={}).status_code == 429

    records = [
        json.loads(record.message) for record in caplog.records
        if record.name == "a2a_hub.audit.workbuddy"
    ]
    assert [item["status"] for item in records] == [401, 403, 200, 429]
    assert [item["outcome"] for item in records] == [
        "unauthenticated", "denied", "ok", "rate_limited"
    ]
    assert records[0]["token_fp"] == "-"
    assert records[1]["token_fp"] == hashlib.sha256(wrong.encode()).hexdigest()[:8]
    assert all("\n" not in record.message for record in caplog.records if record.name == "a2a_hub.audit.workbuddy")
    store.close()



@pytest.mark.parametrize(
    ("suffix", "method", "path", "payload", "expected_first"),
    [
        ("claim", "POST", "/integrations/workbuddy/v1/claim", {}, 200),
        ("start", "POST", "/integrations/workbuddy/v1/tasks/missing-start/start",
         {"lease_token": "private-lease-token-0123456789", "lease_version": 1}, 409),
        ("renew", "POST", "/integrations/workbuddy/v1/tasks/missing-renew/renew",
         {"lease_token": "private-lease-token-0123456789", "lease_version": 1}, 409),
        ("progress", "POST", "/integrations/workbuddy/v1/tasks/missing-progress/progress",
         {"lease_token": "private-lease-token-0123456789", "lease_version": 1,
          "sequence": 1, "message": "test progress"}, 409),
        ("result", "POST", "/integrations/workbuddy/v1/tasks/missing-result/result",
         {"lease_token": "private-lease-token-0123456789", "lease_version": 1,
          "result_key": "result-key-0001", "state": "completed", "output": "test"}, 409),
        ("callback-status", "GET", "/integrations/workbuddy/v1/callbacks/missing-callback",
         None, 404),
        ("callback-retry", "POST", "/integrations/workbuddy/v1/callbacks/missing-callback/retry",
         None, 409),
        ("handshake", "POST", "/integrations/workbuddy/v1/handshake",
         {"connector_id": "placeholder", "protocol_version": "1.0",
          "client_version": "test-client", "capabilities": []}, 200),
        ("heartbeat", "POST", "/integrations/workbuddy/v1/heartbeat",
         {"connector_id": "placeholder", "heartbeat_id": "heartbeat-guard-0001",
          "status": "idle", "current_task_id": None}, 200),
        ("presence", "GET", "/integrations/workbuddy/v1/presence", None, 200),
    ],
)
def test_every_workbuddy_connector_endpoint_enforces_rate_limit(
    client, monkeypatch, tmp_path, suffix, method, path, payload, expected_first
):
    settings, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix=f"endpoint-{suffix}", limit=1
    )
    monkeypatch.setattr(
        settings, "workbuddy_presence_db_path", str(tmp_path / f"{suffix}-presence.db")
    )
    monkeypatch.setattr(
        settings, "github_callback_outbox_db_path", str(tmp_path / f"{suffix}-outbox.db")
    )
    if payload is not None and "connector_id" in payload:
        payload = {**payload, "connector_id": settings.workbuddy_connector_id}

    if suffix == "heartbeat":
        from a2a_hub.workbuddy_presence import WorkBuddyPresenceStore

        WorkBuddyPresenceStore(
            settings.workbuddy_presence_db_path,
            ttl_seconds=settings.workbuddy_presence_ttl_seconds,
        ).record_handshake(
            settings.workbuddy_connector_id, "1.0", "test-client", []
        )

    headers = {"X-WorkBuddy-Connector-Token": token}

    def send():
        if payload is None:
            return client.request(method, path, headers=headers)
        return client.request(method, path, headers=headers, json=payload)

    try:
        first = send()
        second = send()
        assert first.status_code == expected_first
        assert second.status_code == 429, (
            f"{method} {path} did not enforce the shared connector rate limit; "
            f"first={first.status_code}, second={second.status_code}"
        )
    finally:
        store.close()

def test_connector_rate_limit_identity_rotates_with_token_and_connector_id(
    client, monkeypatch, tmp_path
):
    settings, store, token = _configure_connector(
        monkeypatch, tmp_path, suffix="identity-rotation", limit=1
    )
    url = "/integrations/workbuddy/v1/claim"
    old_headers = {"X-WorkBuddy-Connector-Token": token}
    assert client.post(url, headers=old_headers, json={}).status_code == 200

    rotated_token = token + "-rotated"
    monkeypatch.setattr(settings, "workbuddy_connector_token", rotated_token)
    assert client.post(url, headers=old_headers, json={}).status_code == 403
    new_headers = {"X-WorkBuddy-Connector-Token": rotated_token}
    assert client.post(url, headers=new_headers, json={}).status_code == 200

    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-identity-rotated")
    assert client.post(url, headers=new_headers, json={}).status_code == 200
    store.close()


def test_connector_rate_limit_budget_survives_router_recreation(
    client, monkeypatch, tmp_path
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import a2a_hub.server as server
    from a2a_hub.store import SqliteTaskStore
    from a2a_hub.workbuddy_connector import build_workbuddy_connector_router

    settings = server.hub.settings
    token = "test-workbuddy-router-restart-0123456789"
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-router-restart")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 1)
    monkeypatch.setattr(
        settings, "workbuddy_rate_limit_db_path", str(tmp_path / "router-restart-rate.db")
    )
    task_store = SqliteTaskStore(str(tmp_path / "router-restart-tasks.db"))
    monkeypatch.setattr(server.hub.registry, "store", task_store)
    headers = {"X-WorkBuddy-Connector-Token": token}
    url = "/integrations/workbuddy/v1/claim"

    first = client.post(url, headers=headers, json={})
    assert first.status_code == 200

    # A fresh router has an empty in-process cache but must see the same durable budget.
    app = FastAPI()
    app.include_router(build_workbuddy_connector_router(lambda: server.hub))
    with TestClient(app) as restarted_client:
        second = restarted_client.post(url, headers=headers, json={})
    assert second.status_code == 429
    task_store.close()


def test_connector_rate_limit_storage_failure_fails_closed_without_leaking_details(
    client, monkeypatch, tmp_path
):
    import a2a_hub.server as server

    settings = server.hub.settings
    token = "test-workbuddy-storage-failure-0123456789"
    bad_path = tmp_path / "rate-limit-is-a-directory"
    bad_path.mkdir()
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-storage-failure")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 5)
    monkeypatch.setattr(settings, "workbuddy_rate_limit_db_path", str(bad_path))

    response = client.post(
        "/integrations/workbuddy/v1/claim",
        headers={"X-WorkBuddy-Connector-Token": token},
        json={},
    )
    assert response.status_code == 503
    assert str(bad_path) not in response.text
    assert token not in response.text
    assert "暂不可用" in response.text

