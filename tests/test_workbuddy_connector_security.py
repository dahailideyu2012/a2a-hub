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
