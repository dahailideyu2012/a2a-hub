"""Durable cancellation polling for the WorkBuddy connector (T32)."""
from __future__ import annotations


def _prepare(client, monkeypatch, tmp_path, suffix: str):
    import a2a_hub.server as server
    from a2a_hub.models import Message, Task
    from a2a_hub.store import SqliteTaskStore

    settings = server.hub.settings
    token = f"test-workbuddy-cancel-{suffix}-0123456789"
    monkeypatch.setattr(settings, "workbuddy_connector_token", token)
    monkeypatch.setattr(settings, "workbuddy_connector_id", "workbuddy-personal")
    monkeypatch.setattr(settings, "workbuddy_connector_allowed_agents", "echo-a")
    monkeypatch.setattr(
        settings, "workbuddy_presence_db_path", str(tmp_path / f"{suffix}-presence.db")
    )
    store = SqliteTaskStore(str(tmp_path / f"{suffix}-tasks.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)

    task = Task(agentId="unregistered-test-agent", metadata={"mode": "pull"})
    task.history.append(Message.user("cancellation signal test"))
    store.enqueue_pull(task)
    lease = store.claim_next("workbuddy-personal", 120)
    assert lease is not None and lease["task"].id == task.id

    headers = {"X-WorkBuddy-Connector-Token": token}
    handshake = client.post(
        "/integrations/workbuddy/v1/handshake",
        headers=headers,
        json={"connector_id": "workbuddy-personal"},
    )
    assert handshake.status_code == 200
    return settings, store, task, headers


async def test_heartbeat_reports_cancellation_only_after_persisted_cancel(
    client, monkeypatch, tmp_path
):
    _, store, task, headers = _prepare(
        client, monkeypatch, tmp_path, "cancel-transition"
    )
    url = "/integrations/workbuddy/v1/heartbeat"
    base = {
        "connector_id": "workbuddy-personal",
        "heartbeat_id": "heartbeat-before-cancel-001",
        "status": "busy",
        "current_task_id": task.id,
    }

    before = client.post(url, headers=headers, json=base)
    assert before.status_code == 200
    assert before.json()["cancel_requested"] is False
    assert before.json()["cancel_reason"] is None

    import a2a_hub.server as server

    # Exercise the same registry cancellation path used by ChatGPT's REST route.
    canceled = await server.hub.registry.cancel(task.id)
    assert canceled.status.state.value == "canceled"

    after = client.post(
        url,
        headers=headers,
        json={**base, "heartbeat_id": "heartbeat-after-cancel-001"},
    )
    assert after.status_code == 200
    assert after.json()["cancel_requested"] is True
    assert after.json()["cancel_reason"] == "task_cancelled_by_caller"
    store.close()


def test_cancellation_query_is_scoped_to_task_and_connector(tmp_path):
    from a2a_hub.models import Message, Task
    from a2a_hub.store import SqliteTaskStore

    store = SqliteTaskStore(str(tmp_path / "cancel-scope.db"))
    task = Task(agentId="echo-a", metadata={"mode": "pull"})
    task.history.append(Message.user("connector scope"))
    store.enqueue_pull(task)
    lease = store.claim_next("connector-a", 120)
    assert lease is not None
    store.cancel_pull_task(task.id)

    assert store.is_pull_task_cancelled(task.id, "connector-a") is True
    assert store.is_pull_task_cancelled(task.id, "connector-b") is False
    assert store.is_pull_task_cancelled("missing-task-id", "connector-a") is False
    store.close()
