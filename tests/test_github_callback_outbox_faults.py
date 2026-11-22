from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import a2a_hub.github_callback_outbox as outbox_module
from a2a_hub.github_callback_outbox import GithubCallbackOutbox

CALLBACK = {"repository": "owner/repo", "issue_number": 42, "source_comment_id": 9}


def test_process_crash_after_claim_is_recovered_after_restart(tmp_path, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(outbox_module.time, "time", lambda: clock[0])
    path = tmp_path / "crash-after-claim.db"
    first_process = GithubCallbackOutbox(path, lease_seconds=15)
    assert first_process.enqueue("crash-before-send", CALLBACK, "completed", "durable output")

    abandoned = first_process.claim_due()
    assert abandoned is not None
    assert abandoned["attempts"] == 1

    restarted_process = GithubCallbackOutbox(path, lease_seconds=15)
    assert restarted_process.claim_due() is None
    clock[0] += 16
    recovered = restarted_process.claim_due()

    assert recovered is not None
    assert recovered["task_id"] == abandoned["task_id"]
    assert recovered["callback"] == CALLBACK
    assert recovered["output"] == "durable output"
    assert recovered["attempts"] == 2
    assert recovered["lease_token"] != abandoned["lease_token"]
    assert restarted_process.mark_delivered(recovered["task_id"], recovered["lease_token"])


def test_remote_accept_then_process_crash_replays_idempotently(tmp_path, monkeypatch):
    clock = [2_000.0]
    monkeypatch.setattr(outbox_module.time, "time", lambda: clock[0])
    path = tmp_path / "crash-after-remote-accept.db"
    first_process = GithubCallbackOutbox(path, lease_seconds=15)
    first_process.enqueue("ambiguous-post", CALLBACK, "completed", "same result")
    first_claim = first_process.claim_due()

    accepted_comments: dict[str, str] = {}
    post_attempts = []

    def idempotent_remote_post(item):
        post_attempts.append(item["task_id"])
        accepted_comments.setdefault(item["task_id"], item["output"])

    idempotent_remote_post(first_claim)
    # Simulate process death: deliberately do not acknowledge the first lease.

    restarted_process = GithubCallbackOutbox(path, lease_seconds=15)
    clock[0] += 16
    second_claim = restarted_process.claim_due()
    assert second_claim is not None
    assert second_claim["lease_token"] != first_claim["lease_token"]
    idempotent_remote_post(second_claim)

    assert post_attempts == ["ambiguous-post", "ambiguous-post"]
    assert accepted_comments == {"ambiguous-post": "same result"}
    assert restarted_process.mark_delivered(second_claim["task_id"], second_claim["lease_token"])
    assert restarted_process.status("ambiguous-post")["status"] == "delivered"


def test_parallel_claims_distribute_many_tasks_without_duplicate_ownership(tmp_path):
    path = str(tmp_path / "parallel-batch.db")
    seed = GithubCallbackOutbox(path)
    task_ids = [f"parallel-{index:02d}" for index in range(32)]
    for task_id in task_ids:
        assert seed.enqueue(task_id, CALLBACK, "completed", f"output:{task_id}")

    def claim_one(_worker: int):
        return GithubCallbackOutbox(path).claim_due()

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(pool.map(claim_one, range(len(task_ids))))

    winners = [item for item in claims if item is not None]
    claimed_ids = [item["task_id"] for item in winners]
    tokens = [item["lease_token"] for item in winners]
    assert len(winners) == len(task_ids)
    assert len(set(claimed_ids)) == len(task_ids)
    assert set(claimed_ids) == set(task_ids)
    assert len(set(tokens)) == len(tokens)

    for item in winners:
        assert seed.mark_delivered(item["task_id"], item["lease_token"])
    assert all(seed.status(task_id)["status"] == "delivered" for task_id in task_ids)


def test_expired_lease_cannot_be_acknowledged_before_reclaim(tmp_path, monkeypatch):
    clock = [3_000.0]
    monkeypatch.setattr(outbox_module.time, "time", lambda: clock[0])
    box = GithubCallbackOutbox(tmp_path / "expired-ack.db", lease_seconds=15)
    box.enqueue("expired-ack", CALLBACK, "completed", "output")
    claimed = box.claim_due()
    assert claimed is not None

    clock[0] += 16
    assert box.mark_delivered("expired-ack", claimed["lease_token"]) is False
    assert box.status("expired-ack")["status"] == "processing"

    reclaimed = box.claim_due()
    assert reclaimed is not None
    assert reclaimed["lease_token"] != claimed["lease_token"]
    assert box.mark_delivered("expired-ack", reclaimed["lease_token"])
