"""WorkBuddy pull connector and lease fencing tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from a2a_hub.models import TaskState
from a2a_hub.store import SqliteTaskStore


def rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": "connector-test", "method": method, "params": params or {}}


@pytest.mark.asyncio
async def test_enqueue_requires_persistent_store(dispatcher):
    response = await dispatcher.handle(rpc("tasks/enqueue", {
        "agentId": "echo-a", "message": "must not run",
        "metadata": {"source": "chatgpt-ingress", "mode": "pull"},
    }))
    assert response["error"]["code"] == -32603


@pytest.mark.asyncio
async def test_enqueue_persists_without_starting_adapter(dispatcher, registry, tmp_path, monkeypatch):
    store = SqliteTaskStore(str(tmp_path / "pull.db"))
    monkeypatch.setattr(registry, "store", store)

    def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("Pull enqueue must not execute the Hub adapter")

    monkeypatch.setattr(registry, "execute", forbidden_execute)
    response = await dispatcher.handle(rpc("tasks/enqueue", {
        "agentId": "echo-a", "message": "queued only",
        "metadata": {"source": "chatgpt-ingress", "mode": "pull"},
    }))
    assert "error" not in response
    task_id = response["result"]["id"]
    persisted = store.get(task_id)
    assert persisted is not None
    assert persisted.status.state == TaskState.SUBMITTED
    assert store.claim_next("workbuddy-test", 120)["task"].id == task_id
    assert store.claim_next("workbuddy-test", 120) is None
    store.close()


def test_concurrent_claim_has_exactly_one_winner(tmp_path):
    path = str(tmp_path / "race.db")
    seed = SqliteTaskStore(path)
    from a2a_hub.models import Message, Task
    task = Task(agentId="echo-a", metadata={"mode": "pull"})
    task.history.append(Message.user("race"))
    seed.enqueue_pull(task)
    seed.close()

    def claim(connector: str):
        store = SqliteTaskStore(path)
        try:
            return store.claim_next(connector, 120)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["connector-a", "connector-b"]))
    assert sum(result is not None for result in results) == 1


def test_expired_lease_fences_old_connector_and_result_is_idempotent(tmp_path):
    import sqlite3
    import time
    from a2a_hub.models import Message, Task

    path = str(tmp_path / "fencing.db")
    store = SqliteTaskStore(path)
    task = Task(agentId="echo-a")
    task.history.append(Message.user("fencing"))
    store.enqueue_pull(task)
    first = store.claim_next("connector-old", 120)
    assert first is not None

    conn = sqlite3.connect(path)
    conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, task.id))
    conn.commit()
    conn.close()

    second = store.claim_next("connector-new", 120)
    assert second is not None
    assert second["lease_version"] == first["lease_version"] + 1
    with pytest.raises(ValueError, match="lease_conflict"):
        store.progress_pull_task(
            task.id, "connector-old", first["lease_token"], first["lease_version"], 1, "stale"
        )

    result, replay = store.finish_pull_task(
        task.id, "connector-new", second["lease_token"], second["lease_version"],
        "result-key-0001", "completed", "done",
    )
    assert result.status.state == TaskState.COMPLETED
    assert replay is False
    repeated, replay = store.finish_pull_task(
        task.id, "connector-new", second["lease_token"], second["lease_version"],
        "result-key-0001", "completed", "done",
    )
    assert repeated.id == task.id
    assert replay is True
    store.close()


def test_pull_cancel_is_store_only_and_terminal(tmp_path):
    from a2a_hub.models import Message, Task

    store = SqliteTaskStore(str(tmp_path / "cancel.db"))
    task = Task(agentId="echo-a")
    task.history.append(Message.user("cancel me"))
    store.enqueue_pull(task)
    canceled = store.cancel_pull_task(task.id)
    assert canceled.status.state == TaskState.CANCELED
    assert store.get(task.id).status.state == TaskState.CANCELED
    with pytest.raises(ValueError, match="task_terminal"):
        store.cancel_pull_task(task.id)
    store.close()


def test_connector_auth_and_memory_store_fail_closed(client):
    missing = client.post("/integrations/workbuddy/v1/claim", json={})
    assert missing.status_code == 401
    denied = client.post(
        "/integrations/workbuddy/v1/claim",
        headers={"X-WorkBuddy-Connector-Token": "test-workbuddy-connector-token-0123456789"},
        json={},
    )
    assert denied.status_code == 503


def test_chatgpt_pull_to_workbuddy_result_roundtrip(client, tmp_path, monkeypatch):
    import a2a_hub.server as server

    store = SqliteTaskStore(str(tmp_path / "roundtrip.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)

    def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("Pull mode must not start the Hub adapter")

    monkeypatch.setattr(server.hub.registry, "execute", forbidden_execute)
    submitted = client.post(
        "/integrations/chatgpt/v1/tasks",
        headers={
            "X-ChatGPT-Ingress-Token": "test-chatgpt-ingress-token-0123456789",
            "Idempotency-Key": "pull-roundtrip-001",
        },
        json={"agent_id": "echo-a", "prompt": "execute on WorkBuddy", "mode": "pull"},
    )
    assert submitted.status_code == 200
    task_id = submitted.json()["task"]["id"]
    assert submitted.json()["task"]["status"]["state"] == "submitted"

    token = "test-workbuddy-connector-token-0123456789"
    headers = {"X-WorkBuddy-Connector-Token": token}
    claimed = client.post("/integrations/workbuddy/v1/claim", headers=headers, json={})
    assert claimed.status_code == 200
    assert claimed.json()["claimed"] is True
    assert claimed.json()["task"]["id"] == task_id
    lease = claimed.json()["lease"]
    lease_body = {"lease_token": lease["lease_token"], "lease_version": lease["lease_version"]}

    started = client.post(f"/integrations/workbuddy/v1/tasks/{task_id}/start", headers=headers, json=lease_body)
    assert started.status_code == 200
    progress = client.post(
        f"/integrations/workbuddy/v1/tasks/{task_id}/progress", headers=headers,
        json={**lease_body, "sequence": 1, "message": "running"},
    )
    assert progress.status_code == 200
    result_body = {**lease_body, "result_key": "roundtrip-result-01", "state": "completed", "output": "WorkBuddy finished"}
    result = client.post(f"/integrations/workbuddy/v1/tasks/{task_id}/result", headers=headers, json=result_body)
    assert result.status_code == 200
    assert result.json()["task"]["status"]["state"] == "completed"
    replay = client.post(f"/integrations/workbuddy/v1/tasks/{task_id}/result", headers=headers, json=result_body)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    readback = client.get(
        f"/integrations/chatgpt/v1/tasks/{task_id}",
        headers={"X-ChatGPT-Ingress-Token": "test-chatgpt-ingress-token-0123456789"},
    )
    assert readback.status_code == 200
    assert "WorkBuddy finished" in str(readback.json())
    store.close()
