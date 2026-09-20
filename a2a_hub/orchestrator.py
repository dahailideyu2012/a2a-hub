"""多 Agent 协同编排器。

A2A 解决的是"agent 之间怎么说话"，本模块解决的是"agent 之间怎么干活"。
四种经过实践检验的协同拓扑：

  delegate   委派       —— 按能力路由到唯一最合适的 agent；可选叠加评审人
  broadcast  广播       —— N 个 agent 并行处理同一问题，再由综合者收敛
  pipeline   流水线     —— 串行接力，上游产出即下游输入（调研→写作→润色）
  roundtable 圆桌       —— 多轮讨论，每轮 agent 看到他人观点后修订，最后收敛

所有模式共享同一套事件流，前端/CLI 可以统一渲染协同过程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, Field

from .bus import STREAM_END, EventBus
from .models import Task, TaskState, new_id, utc_now
from .registry import AgentRecord, AgentRegistry

log = logging.getLogger("a2a_hub.orchestrator")

DEFAULT_STAGE_TEMPLATES = {
    "delegate": "{prompt}",
    "broadcast": "{prompt}",
    "pipeline": "{prompt}",
    "roundtable": "{prompt}",
}


class StepRecord(BaseModel):
    stepId: str = Field(default_factory=lambda: new_id("step-"))
    label: str = ""
    agentId: str = ""
    agentName: str = ""
    state: str = "pending"
    taskId: Optional[str] = None
    prompt: str = ""
    output: str = ""
    error: Optional[str] = None
    durationMs: int = 0
    round: int = 0


class CollaborationRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("collab-"))
    mode: str
    prompt: str
    status: str = "pending"
    agentIds: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    result: str = ""
    error: Optional[str] = None
    createdAt: str = Field(default_factory=utc_now)
    updatedAt: str = Field(default_factory=utc_now)
    options: dict[str, Any] = Field(default_factory=dict)


MODES = ("delegate", "broadcast", "pipeline", "roundtable")


class Orchestrator:
    def __init__(self, registry: AgentRegistry, bus: EventBus) -> None:
        self.registry = registry
        self.bus = bus
        self._runs: dict[str, CollaborationRun] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------ #
    # 运行管理
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> Optional[CollaborationRun]:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 50) -> list[CollaborationRun]:
        ids = list(reversed(self._order))[:limit]
        return [self._runs[i] for i in ids if i in self._runs]

    def _channel(self, run_id: str) -> str:
        return f"collab:{run_id}"

    async def _publish(self, run: CollaborationRun, event_type: str, **payload: Any) -> None:
        run.updatedAt = utc_now()
        self.bus.publish(
            self._channel(run.id),
            {
                "kind": "collab-event",
                "event": event_type,
                "runId": run.id,
                "mode": run.mode,
                "status": run.status,
                "ts": run.updatedAt,
                **payload,
            },
        )

    def create_run(
        self,
        mode: str,
        prompt: str,
        agent_ids: Optional[list[str]] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> CollaborationRun:
        if mode not in MODES:
            raise ValueError(f"不支持的协同模式 `{mode}`，可选：{', '.join(MODES)}")
        run = CollaborationRun(
            mode=mode,
            prompt=prompt,
            agentIds=list(agent_ids or []),
            options=options or {},
        )
        self._runs[run.id] = run
        self._order.append(run.id)
        while len(self._order) > 200:
            old = self._order.pop(0)
            self._runs.pop(old, None)
        return run

    async def launch(self, run: CollaborationRun) -> CollaborationRun:
        """异步启动（HTTP/Web 场景）。"""
        task = asyncio.create_task(self.execute(run))
        self._tasks[run.id] = task
        return run

    async def run_sync(self, run: CollaborationRun) -> CollaborationRun:
        """阻塞执行（CLI 场景）。"""
        await self.execute(run)
        return run

    async def cancel_run(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        run = self._runs.get(run_id)
        if run is None:
            return False
        run.status = "canceled"
        if task and not task.done():
            task.cancel()
        for step in run.steps:
            if step.taskId and step.state == "working":
                try:
                    await self.registry.cancel(step.taskId)
                except Exception:  # noqa: BLE001
                    pass
        await self._publish(run, "collab-canceled")
        return True

    #: 协同运行已结束的状态
    TERMINAL_RUN_STATES = ("completed", "failed", "canceled")

    async def stream(self, run_id: str, heartbeat: float = 20.0) -> AsyncIterator[dict[str, Any]]:
        """订阅一次协同运行的全过程事件。

        **必须先订阅再发快照**：否则「取快照」与「挂上订阅」之间的窗口里
        产生的事件会被永久漏掉（返回很快的 agent 尤其容易撞上）。
        空闲 heartbeat 秒只发心跳、不结束流，保证长任务一直连着。
        """
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)

        q = self.bus.subscribe(self._channel(run_id))
        try:
            yield {
                "kind": "collab-event",
                "event": "collab-snapshot",
                "run": run.model_dump(mode="json"),
            }
            if run.status in self.TERMINAL_RUN_STATES:
                yield {"kind": "collab-event", "event": "collab-end", "runId": run_id}
                return

            while True:
                got, item = await self.bus.next_event(q, heartbeat)
                if not got:
                    # 空闲心跳；顺便确认运行是否已结束，避免流悬挂
                    if run.status in self.TERMINAL_RUN_STATES:
                        break
                    yield {"kind": "collab-event", "event": "collab-heartbeat", "runId": run_id}
                    continue
                if item is STREAM_END:
                    break
                yield item if isinstance(item, dict) else {"kind": "raw", "data": str(item)}
        finally:
            self.bus.unsubscribe(self._channel(run_id), q)
        yield {"kind": "collab-event", "event": "collab-end", "runId": run_id}

    # ------------------------------------------------------------------ #
    # 执行主流程
    # ------------------------------------------------------------------ #

    async def execute(self, run: CollaborationRun) -> CollaborationRun:
        run.status = "running"
        await self._publish(run, "collab-started")
        try:
            handler = getattr(self, f"_run_{run.mode}")
            await handler(run)
            if run.status == "running":
                run.status = "completed"
        except asyncio.CancelledError:
            run.status = "canceled"
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("协同任务 %s 失败", run.id)
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._publish(run, "collab-finished", result=run.result, error=run.error)
            self.bus.close_task(self._channel(run.id))
            self._tasks.pop(run.id, None)
        return run

    # ------------------------------------------------------------------ #
    # 单步执行
    # ------------------------------------------------------------------ #

    async def _step(
        self,
        run: CollaborationRun,
        record: AgentRecord,
        prompt: str,
        label: str,
        round_no: int = 0,
    ) -> StepRecord:
        step = StepRecord(
            label=label,
            agentId=record.id,
            agentName=record.name,
            state="working",
            prompt=prompt,
            round=round_no,
        )
        run.steps.append(step)
        await self._publish(run, "step-started", step=step.model_dump(mode="json"))

        started = time.perf_counter()
        task: Optional[Task] = None
        try:
            task = await self.registry.run_blocking(
                record.id,
                prompt,
                context_id=run.id,
                metadata={"collabRunId": run.id, "collabMode": run.mode, "label": label},
            )
            step.taskId = task.id
            text = task.final_text()
            if not text.strip() and task.status.message is not None:
                text = task.status.message.text()
            step.output = text
            if task.status.state == TaskState.COMPLETED:
                step.state = "completed"
            elif task.status.state == TaskState.CANCELED:
                step.state = "canceled"
            else:
                step.state = "failed"
                step.error = task.status.message.text() if task.status.message else task.status.state.value
        except Exception as exc:  # noqa: BLE001
            step.state = "failed"
            step.error = f"{type(exc).__name__}: {exc}"
        finally:
            step.durationMs = int((time.perf_counter() - started) * 1000)

        await self._publish(run, "step-finished", step=step.model_dump(mode="json"))
        return step

    def _pick_agents(
        self, run: CollaborationRun, prompt: str, default_k: int = 3
    ) -> list[AgentRecord]:
        """确定参与本次协同的 agent 列表。"""
        if run.agentIds:
            picked: list[AgentRecord] = []
            for aid in run.agentIds:
                if self.registry.has(aid):
                    picked.append(self.registry.get(aid))
            if picked:
                return picked
        k = int(run.options.get("topK", default_k))
        ranked = self.registry.rank(prompt, top_k=max(1, k))
        selected = [r for r, _ in ranked]
        if not selected:
            enabled = [r for r in self.registry.list_records() if r.spec.enabled]
            selected = enabled[: max(1, k)]
        return selected

    async def _synthesize(self, run: CollaborationRun, sections: list[tuple[str, str]]) -> str:
        """把多份产出收敛成一份结论。

        有指定 synthesizer agent 就走 LLM 综合（并在时间线上显示为一步）；
        否则退化为结构化拼接——保证「即使一个模型都调不通，协同结果依然可用」。
        """
        valid = [(n, t) for n, t in sections if t and t.strip()]
        if not valid:
            return ""
        if len(valid) == 1:
            return valid[0][1]

        synth_id = run.options.get("synthesizer")
        if synth_id and self.registry.has(synth_id):
            body = "\n\n".join(
                f"### 方案 {i}（来自 {n}）\n{t}" for i, (n, t) in enumerate(valid, 1)
            )
            prompt = (
                "你是多智能体协同的综合分析者。以下是多个 AI agent 针对同一问题的独立回答，"
                "请输出一份融合结论：先给最终答案，再指出各方案的关键分歧与取舍理由。"
                "不要罗列谁说了什么，要给判断。\n\n"
                f"【原始问题】\n{run.prompt}\n\n{body}"
            )
            step = await self._step(
                run,
                self.registry.get(synth_id),
                prompt,
                label="综合",
                round_no=max((s.round for s in run.steps), default=0),
            )
            if step.state == "completed" and step.output.strip():
                return step.output
            log.warning("综合者 %s 调用失败，降级为结构化拼接：%s", synth_id, step.error)

        parts = [
            "## 协同结论（结构化汇总）",
            f"参与 agent：{len(valid)} 个 · 模式：{run.mode}",
            "",
        ]
        for i, (name, text) in enumerate(valid, 1):
            parts.append(f"### {i}. {name}")
            parts.append(text.strip())
            parts.append("")
        parts.append("> 未配置综合者 agent，以上为各 agent 原始产出汇总。")
        parts.append("> 在协作参数中指定 `synthesizer` 可启用 LLM 融合。")
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # 模式一：delegate —— 按能力委派
    # ------------------------------------------------------------------ #

    async def _run_delegate(self, run: CollaborationRun) -> None:
        picked = self._pick_agents(run, run.prompt, default_k=1)
        if not picked:
            raise ValueError("没有可用的 agent 可以执行委派")
        target = picked[0]
        await self._publish(run, "plan", plan={"steps": [{"agent": target.id, "role": "executor"}]})
        step = await self._step(run, target, run.prompt, label="执行", round_no=0)
        run.result = step.output or step.error or ""

        reviewer_id = run.options.get("reviewer")
        if reviewer_id and self.registry.has(reviewer_id) and step.state == "completed":
            reviewer = self.registry.get(reviewer_id)
            review_prompt = (
                f"请评审下面这份产出，指出具体问题并给出改进后的版本。\n\n"
                f"【原始任务】\n{run.prompt}\n\n【待评审产出】\n{step.output}"
            )
            review = await self._step(run, reviewer, review_prompt, label="评审", round_no=0)
            run.result = (review.output or review.error or "").strip() or run.result

    # ------------------------------------------------------------------ #
    # 模式二：broadcast —— 并行广播 + 收敛
    # ------------------------------------------------------------------ #

    async def _run_broadcast(self, run: CollaborationRun) -> None:
        agents = self._pick_agents(run, run.prompt, default_k=3)
        await self._publish(
            run, "plan", plan={"steps": [{"agent": a.id, "role": "responder"} for a in agents]}
        )
        results = await asyncio.gather(
            *(self._step(run, a, run.prompt, label=f"并行应答·{a.name}") for a in agents),
            return_exceptions=False,
        )
        sections = [(s.agentName or s.agentId, s.output) for s in results]
        run.result = await self._synthesize(run, sections)

    # ------------------------------------------------------------------ #
    # 模式三：pipeline —— 串行接力
    # ------------------------------------------------------------------ #

    async def _run_pipeline(self, run: CollaborationRun) -> None:
        stages = run.options.get("stages")
        if stages:
            chain = [
                (st["agent"], st.get("label", f"阶段{i + 1}"), st.get("template", "{input}"))
                for i, st in enumerate(stages)
            ]
            resolved: list[tuple[AgentRecord, str, str]] = []
            for agent_id, label, template in chain:
                if not self.registry.has(agent_id):
                    raise ValueError(f"流水线阶段引用了不存在的 agent `{agent_id}`")
                resolved.append((self.registry.get(agent_id), label, template))
        else:
            n = int(run.options.get("stagesCount", 3))
            agents = self._pick_agents(run, run.prompt, default_k=n)
            labels = ["调研/分析", "产出/实现", "校核/润色"]
            resolved = [
                (a, labels[i] if i < len(labels) else f"阶段{i + 1}", "{input}")
                for i, a in enumerate(agents)
            ]

        await self._publish(
            run,
            "plan",
            plan={"stages": [{"agent": a.id, "label": lb} for a, lb, _ in resolved]},
        )

        carry = run.prompt
        for idx, (record, label, template) in enumerate(resolved):
            has_placeholder = "{input}" in template or "{prompt}" in template
            if idx == 0:
                # 首阶段：占位符一律填原始需求
                prompt = template.replace("{input}", run.prompt).replace("{prompt}", run.prompt)
            elif has_placeholder:
                # 后续阶段：{input} 接上游产出，{prompt} 接原始目标
                prompt = template.replace("{input}", carry).replace("{prompt}", run.prompt)
            else:
                # 模板是自然语言指令：自动补上「原始目标 + 上游产出」的上下文
                prompt = (
                    f"【原始目标】\n{run.prompt}\n\n"
                    f"【上游产出，请在此基础上继续】\n{carry}\n\n"
                    f"【本阶段要求】\n{template}"
                )
            step = await self._step(run, record, prompt, label=label, round_no=idx)
            if step.state != "completed":
                run.error = f"流水线在「{label}」阶段中断：{step.error}"
                run.result = carry
                run.status = "failed"
                return
            carry = step.output or carry
        run.result = carry

    # ------------------------------------------------------------------ #
    # 模式四：roundtable —— 多轮圆桌
    # ------------------------------------------------------------------ #

    async def _run_roundtable(self, run: CollaborationRun) -> None:
        agents = self._pick_agents(run, run.prompt, default_k=3)
        rounds = max(1, int(run.options.get("rounds", 2)))
        await self._publish(
            run,
            "plan",
            plan={"rounds": rounds, "steps": [{"agent": a.id, "role": "panelist"} for a in agents]},
        )

        transcript: list[dict[str, str]] = []
        latest: dict[str, str] = {}

        for r in range(rounds):
            for agent in agents:
                if r == 0:
                    prompt = run.prompt
                else:
                    others = "\n\n".join(
                        f"【{n} 的上一轮观点】\n{t}" for n, t in latest.items() if n != agent.name
                    )
                    prompt = (
                        f"【原始议题】\n{run.prompt}\n\n"
                        f"【你的上一轮观点】\n{latest.get(agent.name, '')}\n\n"
                        f"{others}\n\n"
                        f"这是第 {r + 1} 轮讨论。请：1) 指出他人观点中你认同与反对的关键点；"
                        f"2) 给出你修订后的最终结论。只输出结论，不要复述他人原文。"
                    )
                step = await self._step(run, agent, prompt, label=f"第{r + 1}轮", round_no=r)
                if step.state == "completed" and step.output.strip():
                    latest[agent.name] = step.output
                    transcript.append({"agent": agent.name, "round": str(r + 1), "text": step.output})
            await self._publish(run, "round-finished", round=r + 1, participants=list(latest))

        sections = [(n, t) for n, t in latest.items()]
        run.result = await self._synthesize(run, sections)
        run.options["transcript"] = transcript


__all__ = ["Orchestrator", "CollaborationRun", "StepRecord", "MODES"]
