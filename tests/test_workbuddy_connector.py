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


def test_result_replay_succeeds_after_original_lease_expiry(tmp_path):
    import sqlite3
    import time
    from a2a_hub.models import Message, Task

    path = str(tmp_path / "expired-replay.db")
    store = SqliteTaskStore(path)
    task = Task(agentId="echo-a")
    task.history.append(Message.user("replay"))
    store.enqueue_pull(task)
    lease = store.claim_next("workbuddy-personal", 120)
    assert lease is not None
    first, replay = store.finish_pull_task(
        task.id, "workbuddy-personal", lease["lease_token"], lease["lease_version"],
        "stable-result-key", "completed", "durable result",
    )
    assert first.status.state == TaskState.COMPLETED and replay is False

    conn = sqlite3.connect(path)
    conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, task.id))
    conn.commit()
    conn.close()

    repeated, replay = store.finish_pull_task(
        task.id, "workbuddy-personal", lease["lease_token"], lease["lease_version"],
        "stable-result-key", "completed", "durable result",
    )
    assert repeated.id == task.id and replay is True
    with pytest.raises(ValueError, match="task_terminal"):
        store.finish_pull_task(
            task.id, "another-connector", lease["lease_token"], lease["lease_version"],
            "stable-result-key", "completed", "durable result",
        )
    store.close()


def test_connector_allowlist_fails_closed_and_requested_scope_cannot_expand(client, tmp_path, monkeypatch):
    import a2a_hub.server as server
    from a2a_hub.models import Message, Task

    token = "test-workbuddy-connector-token-0123456789"
    headers = {"X-WorkBuddy-Connector-Token": token}
    server.hub.settings.workbuddy_connector_allowed_agents = ""
    unconfigured = client.post("/integrations/workbuddy/v1/claim", headers=headers, json={})
    assert unconfigured.status_code == 503

    server.hub.settings.workbuddy_connector_allowed_agents = "echo-a"
    store = SqliteTaskStore(str(tmp_path / "scope.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)
    task = Task(agentId="echo-b")
    task.history.append(Message.user("out of scope"))
    store.enqueue_pull(task)
    outside = client.post(
        "/integrations/workbuddy/v1/claim", headers=headers, json={"agent_ids": ["echo-b"]}
    )
    assert outside.status_code == 200
    assert outside.json() == {"claimed": False}
    store.close()


def test_connector_renew_endpoint_and_wrong_token(client, tmp_path, monkeypatch):
    import a2a_hub.server as server

    store = SqliteTaskStore(str(tmp_path / "renew.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)
    from a2a_hub.models import Message, Task
    task = Task(agentId="echo-a")
    task.history.append(Message.user("renew"))
    store.enqueue_pull(task)
    token = "test-workbuddy-connector-token-0123456789"
    wrong = client.post("/integrations/workbuddy/v1/claim",
        headers={"X-WorkBuddy-Connector-Token": "x" * 40}, json={})
    assert wrong.status_code == 403
    headers = {"X-WorkBuddy-Connector-Token": token}
    claimed = client.post("/integrations/workbuddy/v1/claim", headers=headers, json={}).json()
    lease = claimed["lease"]
    renewed = client.post(
        f"/integrations/workbuddy/v1/tasks/{task.id}/renew", headers=headers,
        json={"lease_token": lease["lease_token"], "lease_version": lease["lease_version"]},
    )
    assert renewed.status_code == 200
    assert renewed.json()["lease_expires_at"] > lease["lease_expires_at"]
    store.close()


def test_cancel_fences_old_connector_progress_and_result(tmp_path):
    from a2a_hub.models import Message, Task

    store = SqliteTaskStore(str(tmp_path / "cancel-fence.db"))
    task = Task(agentId="echo-a")
    task.history.append(Message.user("cancel fence"))
    store.enqueue_pull(task)
    lease = store.claim_next("workbuddy-personal", 120)
    assert lease is not None
    store.cancel_pull_task(task.id)
    with pytest.raises(ValueError):
        store.progress_pull_task(
            task.id, "workbuddy-personal", lease["lease_token"], lease["lease_version"], 1, "late"
        )
    with pytest.raises(ValueError):
        store.finish_pull_task(
            task.id, "workbuddy-personal", lease["lease_token"], lease["lease_version"],
            "late-result-key", "completed", "late result",
        )
    store.close()


def test_sqlite_reopen_preserves_pending_task_for_claim(tmp_path):
    from a2a_hub.models import Message, Task

    path = str(tmp_path / "reopen.db")
    first = SqliteTaskStore(path)
    task = Task(agentId="echo-a")
    task.history.append(Message.user("survive restart"))
    first.enqueue_pull(task)
    first.close()

    second = SqliteTaskStore(path)
    claimed = second.claim_next("workbuddy-personal", 120)
    assert claimed is not None
    assert claimed["task"].id == task.id
    assert claimed["lease_version"] == 1
    second.close()


def test_default_chatgpt_ingress_mode_still_uses_hub_adapter(client, monkeypatch):
    import a2a_hub.server as server

    called = []
    def fake_execute(record, task, message):
        called.append((record.id, task.id))
        async def stream():
            if False:
                yield None
        return stream()

    monkeypatch.setattr(server.hub.registry, "execute", fake_execute)
    response = client.post(
        "/integrations/chatgpt/v1/tasks",
        headers={
            "X-ChatGPT-Ingress-Token": "test-chatgpt-ingress-token-0123456789",
            "Idempotency-Key": "default-hub-mode-001",
        },
        json={"agent_id": "echo-a", "prompt": "default hub mode"},
    )
    assert response.status_code == 200
    assert called and called[0][0] == "echo-a"


@pytest.mark.asyncio
async def test_registry_aclose_closes_persistent_task_store(tmp_path):
    import sqlite3
    from a2a_hub.bus import EventBus
    from a2a_hub.config import HubConfig, Settings
    from a2a_hub.registry import AgentRegistry
    from a2a_hub.store import SqliteTaskStore

    store = SqliteTaskStore(str(tmp_path / "registry-shutdown.db"))
    registry = AgentRegistry(
        Settings(_env_file=None),
        HubConfig(agents=[]),
        store,
        EventBus(),
    )
    await registry.aclose()

    # A closed sqlite connection raises ProgrammingError on the next operation.
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")



def test_reclaimed_lease_fences_every_old_worker_operation_after_restart(tmp_path):
    """A restarted MCP process has no private lease; after expiry a new worker reclaims,
    and every mutation from the previous worker must be fenced."""
    import sqlite3
    import time
    from a2a_hub.models import Message, Task

    path = str(tmp_path / "restart-reclaim.db")
    store = SqliteTaskStore(path)
    task = Task(agentId="echo-a")
    task.history.append(Message.user("recover after worker restart"))
    store.enqueue_pull(task)

    old = store.claim_next("workbuddy-before-restart", 120)
    assert old is not None
    old_token, old_version = old["lease_token"], old["lease_version"]

    # Simulate the old worker process disappearing and its lease expiring.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, task.id))
    conn.commit()
    conn.close()

    # New MCP process claims the same durable task with a fenced lease version.
    new = store.claim_next("workbuddy-after-restart", 120)
    assert new is not None
    assert new["task"].id == task.id
    assert new["lease_version"] == old_version + 1
    assert new["lease_token"] != old_token

    stale_calls = [
        lambda: store.renew_pull_lease(task.id, "workbuddy-before-restart", old_token, old_version, 120),
        lambda: store.start_pull_task(task.id, "workbuddy-before-restart", old_token, old_version),
        lambda: store.progress_pull_task(task.id, "workbuddy-before-restart", old_token, old_version, 1, "stale progress"),
        lambda: store.finish_pull_task(task.id, "workbuddy-before-restart", old_token, old_version, "stale-result-key", "completed", "stale result"),
    ]
    for stale_call in stale_calls:
        with pytest.raises(ValueError, match="lease_conflict"):
            stale_call()

    store.start_pull_task(task.id, "workbuddy-after-restart", new["lease_token"], new["lease_version"])
    store.progress_pull_task(task.id, "workbuddy-after-restart", new["lease_token"], new["lease_version"], 1, "recovered worker running")
    completed, replay = store.finish_pull_task(
        task.id, "workbuddy-after-restart", new["lease_token"], new["lease_version"],
        "recovered-result-key", "completed", "recovered after restart",
    )
    assert completed.status.state == TaskState.COMPLETED
    assert replay is False
    store.close()



def test_signed_github_task_result_is_returned_to_original_issue(client, tmp_path, monkeypatch):
    import a2a_hub.server as server
    import a2a_hub.workbuddy_connector as connector
    from a2a_hub.models import Message, Task

    store = SqliteTaskStore(str(tmp_path / "github-result.db"))
    monkeypatch.setattr(server.hub.registry, "store", store)
    task = Task(agentId="echo-a", metadata={
        "github_issue_callback": {
            "repository": "owner/repo",
            "issue_number": 42,
            "source_comment_id": 99,
        }
    })
    task.history.append(Message.user("run from GitHub"))
    store.enqueue_pull(task)
    calls = []

    def fake_delivery(callback, task_id, state, output, token, allowed):
        calls.append((callback, task_id, state, output, token, allowed))
        return {"delivered": True, "replayed": len(calls) > 1}

    monkeypatch.setattr(connector, "deliver_github_issue_result", fake_delivery)
    server.hub.settings.workbuddy_connector_allowed_agents = "echo-a"
    server.hub.settings.github_relay_api_token = "x" * 32
    server.hub.settings.github_relay_allowed_repos = "owner/repo"
    token = "test-workbuddy-connector-token-0123456789"
    headers = {"X-WorkBuddy-Connector-Token": token}
    claimed = client.post("/integrations/workbuddy/v1/claim", headers=headers, json={}).json()
    lease = claimed["lease"]
    body = {
        "lease_token": lease["lease_token"],
        "lease_version": lease["lease_version"],
        "result_key": "github-result-0001",
        "state": "completed",
        "output": "result from WorkBuddy",
    }
    first = client.post(f"/integrations/workbuddy/v1/tasks/{task.id}/result", headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["github_callback"]["delivered"] is True
    replay = client.post(f"/integrations/workbuddy/v1/tasks/{task.id}/result", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert len(calls) == 2
    assert calls[0][0]["issue_number"] == 42
    store.close()
