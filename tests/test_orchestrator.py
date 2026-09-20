"""协同编排器测试 —— 四种模式的行为契约。"""

from __future__ import annotations

import pytest

from a2a_hub.orchestrator import MODES, Orchestrator


@pytest.mark.asyncio
class TestDelegate:
    async def test_routes_to_best_agent(self, orchestrator):
        run = orchestrator.create_run("delegate", "帮我评审这段 Python 代码")
        await orchestrator.run_sync(run)
        assert run.status == "completed"
        assert len(run.steps) == 1
        assert run.steps[0].agentId == "echo-a"
        assert run.result

    async def test_explicit_agent_overrides_routing(self, orchestrator):
        run = orchestrator.create_run("delegate", "随便", ["echo-b"])
        await orchestrator.run_sync(run)
        assert run.steps[0].agentId == "echo-b"

    async def test_reviewer_adds_second_step(self, orchestrator):
        run = orchestrator.create_run(
            "delegate", "写点东西", ["echo-b"], {"reviewer": "echo-a"}
        )
        await orchestrator.run_sync(run)
        assert len(run.steps) == 2
        assert run.steps[1].agentId == "echo-a"
        assert run.steps[1].label == "评审"
        # 评审结果覆盖最终产出
        assert run.result == run.steps[1].output

    async def test_missing_reviewer_is_ignored(self, orchestrator):
        run = orchestrator.create_run(
            "delegate", "写点东西", ["echo-b"], {"reviewer": "ghost"}
        )
        await orchestrator.run_sync(run)
        assert len(run.steps) == 1


@pytest.mark.asyncio
class TestBroadcast:
    async def test_parallel_fanout_and_synthesis(self, orchestrator):
        run = orchestrator.create_run("broadcast", "你好世界", options={"topK": 2})
        await orchestrator.run_sync(run)
        assert run.status == "completed"
        assert len(run.steps) == 2
        # 没有 synthesizer 时降级为结构化汇总
        assert "协同结论" in run.result
        assert "结构化汇总" in run.result

    async def test_all_steps_completed(self, orchestrator):
        run = orchestrator.create_run("broadcast", "并发", options={"topK": 2})
        await orchestrator.run_sync(run)
        assert all(s.state == "completed" for s in run.steps)

    async def test_llm_synthesizer_is_used_when_configured(self, orchestrator):
        run = orchestrator.create_run(
            "broadcast", "话题", options={"topK": 2, "synthesizer": "echo-a"}
        )
        await orchestrator.run_sync(run)
        # 综合者是额外一步（2 个应答 + 1 个综合）
        assert len(run.steps) == 3
        assert run.steps[-1].agentId == "echo-a"


@pytest.mark.asyncio
class TestPipeline:
    async def test_explicit_stages_chain(self, orchestrator):
        run = orchestrator.create_run("pipeline", "做一个功能", options={
            "stages": [
                {"agent": "echo-a", "label": "调研"},
                {"agent": "echo-b", "label": "写作", "template": "基于以下内容写稿：{input}"},
            ]
        })
        await orchestrator.run_sync(run)
        assert run.status == "completed"
        assert [s.label for s in run.steps] == ["调研", "写作"]
        # 下游 prompt 里应包含上游产出
        assert "[A] 做一个功能" in run.steps[1].prompt
        assert run.result == run.steps[1].output

    async def test_auto_stage_selection(self, orchestrator):
        run = orchestrator.create_run("pipeline", "自动流水线", options={"stagesCount": 2})
        await orchestrator.run_sync(run)
        assert len(run.steps) == 2
        assert run.status == "completed"

    async def test_pipeline_aborts_on_stage_failure(self, orchestrator):
        # echo-fail 命中 fail_on=BOOM 会失败；prompt 必须带回该关键词，
        # 因为下游阶段拿到的是上游产出（"[A] 会失败 BOOM"）
        run = orchestrator.create_run("pipeline", "会失败 BOOM", options={
            "stages": [
                {"agent": "echo-a", "label": "正常"},
                {"agent": "echo-fail", "label": "崩溃"},
                {"agent": "echo-b", "label": "不该执行"},
            ]
        })
        await orchestrator.run_sync(run)
        assert run.status == "failed"
        assert len(run.steps) == 2
        assert "崩溃" in (run.error or "")
        assert run.steps[-1].state == "failed"

    async def test_unknown_stage_agent_raises(self, orchestrator):
        run = orchestrator.create_run("pipeline", "x", options={
            "stages": [{"agent": "ghost"}]
        })
        await orchestrator.run_sync(run)
        assert run.status == "failed"
        assert "ghost" in (run.error or "")


@pytest.mark.asyncio
class TestRoundtable:
    async def test_multi_round_discussion(self, orchestrator):
        run = orchestrator.create_run(
            "roundtable", "该不该上微服务", options={"topK": 2, "rounds": 2}
        )
        await orchestrator.run_sync(run)
        assert run.status == "completed"
        # 2 个 agent × 2 轮 = 4 步
        assert len(run.steps) == 4
        assert {s.round for s in run.steps} == {0, 1}

    async def test_later_round_prompt_includes_peer_views(self, orchestrator):
        run = orchestrator.create_run(
            "roundtable", "议题", options={"topK": 2, "rounds": 2}
        )
        await orchestrator.run_sync(run)
        second_round = [s for s in run.steps if s.round == 1]
        assert second_round
        for step in second_round:
            assert "上一轮观点" in step.prompt or "第 2 轮讨论" in step.prompt

    async def test_transcript_recorded(self, orchestrator):
        run = orchestrator.create_run(
            "roundtable", "议题", options={"topK": 2, "rounds": 1}
        )
        await orchestrator.run_sync(run)
        assert isinstance(run.options.get("transcript"), list)
        assert len(run.options["transcript"]) == 2


@pytest.mark.asyncio
class TestRunLifecycle:
    async def test_invalid_mode_rejected(self, orchestrator):
        with pytest.raises(ValueError):
            orchestrator.create_run("telepathy", "x")

    async def test_all_modes_are_implemented(self, orchestrator):
        for mode in MODES:
            assert hasattr(orchestrator, f"_run_{mode}"), f"{mode} 没有实现"

    async def test_list_and_get_runs(self, orchestrator):
        run = orchestrator.create_run("delegate", "一", ["echo-a"])
        await orchestrator.run_sync(run)
        assert orchestrator.get_run(run.id) is run
        assert run in orchestrator.list_runs()

    async def test_events_are_published_to_bus(self, orchestrator):
        run = orchestrator.create_run("delegate", "事件", ["echo-a"])
        seen: list[str] = []

        async def watch() -> None:
            async for ev in orchestrator.stream(run.id):
                if isinstance(ev, dict):
                    seen.append(ev.get("event", ""))

        import asyncio

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        await orchestrator.run_sync(run)
        await asyncio.wait_for(watcher, timeout=5)

        assert "collab-snapshot" in seen
        assert "collab-started" in seen
        assert "step-started" in seen
        assert "step-finished" in seen
        assert "collab-finished" in seen

    async def test_step_duration_recorded(self, orchestrator):
        run = orchestrator.create_run("delegate", "计时", ["echo-a"])
        await orchestrator.run_sync(run)
        assert run.steps[0].durationMs >= 0

    async def test_run_ids_unique(self, orchestrator):
        ids = {orchestrator.create_run("delegate", "x", ["echo-a"]).id for _ in range(15)}
        assert len(ids) == 15
