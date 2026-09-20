"""注册中心测试 —— 能力发现、路由打分、任务生命周期、取消。"""

from __future__ import annotations

import pytest

from a2a_hub.models import A2AError, Message, Task, TaskState


def test_registry_loads_configured_agents(registry):
    ids = {r.id for r in registry.list_records()}
    assert {"echo-a", "echo-b", "echo-fail", "static-card"} <= ids


def test_hub_card_aggregates_skills(registry):
    card = registry.hub_card()
    assert card["protocolVersion"] == "0.3.0"
    assert card["capabilities"]["streaming"] is True
    assert card["metadata"]["agentCount"] == 4
    skill_ids = [s["id"] for s in card["skills"]]
    # 内置能力
    assert "multi-agent-collaboration" in skill_ids
    assert "agent-discovery" in skill_ids
    # 子 agent 技能以 agentId:skillId 命名空间化，避免撞车
    assert "echo-a:code-review" in skill_ids


def test_agent_card_lookup(registry):
    card = registry.agent_card("echo-a")
    assert card["name"] == "回显 A"
    assert card["url"].endswith("/agents/echo-a/")


def test_agent_card_for_unknown_agent_raises(registry):
    with pytest.raises(A2AError) as exc:
        registry.agent_card("nope")
    assert "未找到 agent" in str(exc.value)


def test_scoring_prefers_tag_and_description_match(registry):
    review_query = "帮我评审这段代码，看看有没有安全问题"
    writing_query = "帮我写一份年度总结报告"

    code_agent = registry.get("echo-a")
    write_agent = registry.get("echo-b")

    assert registry.score(code_agent, review_query) > registry.score(write_agent, review_query)
    assert registry.score(write_agent, writing_query) > registry.score(code_agent, writing_query)


def test_route_excludes_non_auto_route_agents(registry):
    # echo-fail 的 auto_route=false，永远不应被自动选中
    for _ in range(5):
        rec = registry.route("随便什么问题")
        assert rec is not None
        assert rec.id != "echo-fail"


def test_route_returns_none_when_only_disabled(registry):
    for rec in registry.list_records():
        rec.spec.auto_route = False
    assert registry.route("任意") is None


def test_rank_returns_sorted_candidates(registry):
    ranked = registry.rank("代码评审", top_k=3)
    assert ranked
    assert ranked[0][0].id == "echo-a"
    assert ranked[0][1] >= ranked[-1][1]


@pytest.mark.asyncio
async def test_run_blocking_completes_and_persists(registry):
    task = await registry.run_blocking("echo-a", "写点东西")
    assert task.status.state == TaskState.COMPLETED
    assert task.final_text().startswith("[A]")
    assert registry.store.get(task.id) is not None
    assert task.agentId == "echo-a"


@pytest.mark.asyncio
async def test_run_blocking_failure_surfaces_state(registry):
    task = await registry.run_blocking("echo-fail", "trigger BOOM")
    assert task.status.state == TaskState.FAILED
    assert "BOOM" in task.status.message.text()


@pytest.mark.asyncio
async def test_execute_emits_events_in_order(registry):
    rec = registry.get("echo-a")
    msg = Message.user("顺序测试")
    task = registry.new_task("echo-a", msg)

    kinds = []
    async for event in registry.execute(rec, task, msg):
        kinds.append((event.kind, getattr(event, "final", None)))

    assert kinds[0][0] == "status-update"
    assert kinds[-1] == ("status-update", True)
    assert any(k == "artifact-update" for k, _ in kinds)


@pytest.mark.asyncio
async def test_task_history_and_context_flow(registry):
    rec = registry.get("echo-a")
    msg = Message.user("上下文测试", contextId="ctx-123")
    task = registry.new_task("echo-a", msg, context_id="ctx-123")
    assert task.contextId == "ctx-123"
    assert msg.taskId == task.id
    assert len(task.history) == 1


@pytest.mark.asyncio
async def test_cancel_unknown_task_raises(registry):
    with pytest.raises(A2AError):
        await registry.cancel("task-does-not-exist")


@pytest.mark.asyncio
async def test_cancel_terminal_task_rejected(registry):
    task = await registry.run_blocking("echo-a", "已完成")
    with pytest.raises(A2AError) as exc:
        await registry.cancel(task.id)
    assert "终态" in str(exc.value)


@pytest.mark.asyncio
async def test_health_check_marks_echo_healthy(registry):
    res = await registry.check_health(force=True)
    assert res["echo-a"]["status"] == "healthy"


def test_new_task_generates_unique_ids(registry):
    ids = {registry.new_task("echo-a", Message.user("x")).id for _ in range(20)}
    assert len(ids) == 20


def test_snapshot_shape(registry):
    snap = registry.snapshot()
    a = next(s for s in snap if s["id"] == "echo-a")
    assert {"id", "name", "type", "health", "skills", "url", "stats"} <= set(a)
