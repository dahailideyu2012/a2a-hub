from __future__ import annotations
import asyncio, sqlite3, time
import pytest
from a2a_hub.github_callback_outbox import GithubCallbackOutbox, run_outbox_worker
from a2a_hub.github_result_callback import GithubResultDeliveryError
CALLBACK={"repository":"owner/repo","issue_number":42,"source_comment_id":9}

def test_outbox_persists_and_reclaims_expired_lease(tmp_path):
    path=tmp_path/"outbox.db"; box=GithubCallbackOutbox(path)
    assert box.enqueue("task-1",CALLBACK,"completed","done")
    assert not box.enqueue("task-1",CALLBACK,"completed","done")
    item=box.claim_due(); assert item["attempts"]==1
    with sqlite3.connect(path) as c: c.execute("UPDATE github_callback_outbox SET lease_until=?",(time.time()-1,))
    again=GithubCallbackOutbox(path).claim_due()
    assert again["task_id"]=="task-1" and again["attempts"]==2

def test_backoff_dead_letter_and_manual_retry(tmp_path):
    box=GithubCallbackOutbox(tmp_path/"outbox.db",max_attempts=2,base_backoff=1)
    box.enqueue("task-2",CALLBACK,"failed","oops")
    one=box.claim_due(); box.mark_failed("task-2",one["attempts"],GithubResultDeliveryError("safe"))
    assert box.status("task-2")["status"]=="pending"
    with sqlite3.connect(box.path) as c: c.execute("UPDATE github_callback_outbox SET next_attempt_at=?",(time.time()-1,))
    two=box.claim_due(); box.mark_failed("task-2",two["attempts"],RuntimeError("private payload"))
    assert box.status("task-2")["status"]=="dead"
    assert box.retry("task-2") and box.status("task-2")["attempts"]==0
    assert "private payload" not in str(box.status("task-2"))

@pytest.mark.asyncio
async def test_worker_delivers_and_marks_success(tmp_path):
    box=GithubCallbackOutbox(tmp_path/"outbox.db"); box.enqueue("task-3",CALLBACK,"completed","done")
    calls=[]
    def fake(*args): calls.append(args); return {"delivered":True}
    stop=asyncio.Event()
    worker=asyncio.create_task(run_outbox_worker(box,stop,api_token="synthetic",allowed_repos="owner/repo",poll_seconds=.05,deliver=fake))
    for _ in range(50):
        if box.status("task-3")["status"]=="delivered": break
        await asyncio.sleep(.02)
    stop.set(); await worker
    assert len(calls)==1 and box.status("task-3")["status"]=="delivered"

@pytest.mark.asyncio
async def test_worker_retries_transient_failure(tmp_path):
    box=GithubCallbackOutbox(tmp_path/"outbox.db",base_backoff=1)
    box.enqueue("task-4",CALLBACK,"completed","done"); calls=[]
    def flaky(*args):
        calls.append(1)
        if len(calls)==1: raise GithubResultDeliveryError("transient")
        return {"delivered":True}
    stop=asyncio.Event()
    worker=asyncio.create_task(run_outbox_worker(box,stop,api_token="synthetic",allowed_repos="owner/repo",poll_seconds=.02,deliver=flaky))
    await asyncio.sleep(1.2); stop.set(); await worker
    assert len(calls)>=2 and box.status("task-4")["status"]=="delivered"
