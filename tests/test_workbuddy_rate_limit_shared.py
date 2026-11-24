from __future__ import annotations


def test_workbuddy_endpoints_share_one_persisted_budget(client, monkeypatch, tmp_path):
    import a2a_hub.server as server
    from a2a_hub.store import SqliteTaskStore

    settings = server.hub.settings
    token = "test-shared-workbuddy-rate-limit-token-012345"
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "shared-budget-test")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(settings, "workbuddy_connector_rate_limit", 1)
    monkeypatch.setattr(settings, "workbuddy_rate_limit_db_path", str(tmp_path / "shared-rate.db"))
    task_store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server.hub.registry, "store", task_store)
    monkeypatch.setattr(
        settings, "workbuddy_presence_db_path", str(tmp_path / "presence.db")
    )

    headers = {"X-WorkBuddy-Connector-Token": token}
    first = client.post(
        "/integrations/workbuddy/v1/claim", headers=headers, json={}
    )
    second = client.get(
        "/integrations/workbuddy/v1/presence", headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 429
    task_store.close()
