"""WorkBuddy connector security controls (T31)."""
from __future__ import annotations


def _configure_connector(monkeypatch, tmp_path, *, suffix: str, limit: int):
    import a2a_hub.server as server
    from a2a_hub.store import SqliteTaskStore

    settings = server.hub.settings
    token = f"test-workbuddy-rate-limit-{suffix}-0123456789"
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", f"workbuddy-{suffix}")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", limit)
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

    # A changed limit creates a distinct bucket; restoring the old configuration
    # returns to its still-full bucket rather than silently resetting it.
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 2)
    third = client.post(url, headers=headers, json={})
    assert third.status_code == 200
    assert calls == 2
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 1)
    fourth = client.post(url, headers=headers, json={})
    assert fourth.status_code == 429
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
        assert "\\n" not in record.message
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
    assert all("\\n" not in record.message for record in caplog.records if record.name == "a2a_hub.audit.workbuddy")
    store.close()
