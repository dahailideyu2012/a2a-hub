"""Agent 注册中心 —— 能力发现、健康检查、任务执行与能力路由。

对应 A2A 协议里的 "Agent 目录 / Discovery" 角色：
  * 管理所有适配器实例
  * 发布统一的 Agent Card（Hub 自身一张 + 每个子 agent 一张）
  * 承接任务：落库 -> 广播 working -> 驱动适配器 -> 逐事件落库并推 SSE
  * 按 skill 标签 / 描述关键词给 agent 打分，支持 delegate 自动路由
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, Optional

from . import __version__ as APP_VERSION
from .adapters import BaseAdapter, TaskContext, build_adapter
from .bus import EventBus
from .config import AgentSpec, HubConfig, Settings
from .models import (
    A2AError,
    AgentCard,
    AgentSkill,
    Artifact,
    JsonRpcErrorCodes,
    Message,
    Task,
    TaskEvent,
    TaskState,
    TextPart,
)
from .store import TaskStore

log = logging.getLogger("a2a_hub.registry")

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> set[str]:
    """轻量分词：ASCII 词 + 中文 2/3-gram。

    中文没有空格，若直接按最大连续汉字串切，会得到「帮我评审这段代码」
    这种超长 token，几乎匹配不上任何技能标签。改用重叠 n-gram
    （"评审"、"代码"、"安全问题"…），零依赖就能达到够用的召回率。
    需要更高精度时可整体替换为 jieba / 向量检索。
    """
    low = text.lower()
    tokens: set[str] = set(_ASCII.findall(low))
    for run in _CJK.findall(low):
        if len(run) == 1:
            tokens.add(run)
            continue
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                tokens.add(run[i : i + n])
    return tokens


class AgentRecord:
    """一个已注册 agent 的运行时状态。"""

    def __init__(self, spec: AgentSpec, adapter: BaseAdapter) -> None:
        self.spec = spec
        self.adapter = adapter
        self.health: dict[str, Any] = {"status": "unknown", "detail": "尚未探测"}
        self.checked_at: float = 0.0
        self.task_count: int = 0
        self.error_count: int = 0

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def type(self) -> str:
        return self.spec.type

    @property
    def description(self) -> str:
        return self.spec.description

    def snapshot(self, base_url: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.spec.name,
            "type": self.spec.type,
            "description": self.spec.description,
            "enabled": self.spec.enabled,
            "priority": self.spec.priority,
            "autoRoute": self.spec.auto_route,
            "tags": self.spec.tags,
            "skills": [s.model_dump() for s in self.adapter.skills],
            "transport": self.spec.transport,
            "url": f"{base_url.rstrip('/')}/agents/{self.id}/",
            "health": self.health,
            "stats": {"tasks": self.task_count, "errors": self.error_count},
            "streaming": self.adapter.capabilities().streaming,
        }


class AgentRegistry:
    def __init__(
        self,
        settings: Settings,
        hub_config: HubConfig,
        store: TaskStore,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.hub_config = hub_config
        self.store = store
        self.bus = bus
        self._records: dict[str, AgentRecord] = {}
        #: 社交简报生成器 ``fn(agent_id) -> str``。None/空串 = 不注入，
        #: 社交层关闭时 prompt 逐字节不变。见 relations.social_briefing。
        self._briefing_resolver: Optional[Callable[[str], str]] = None
        self._lock = asyncio.Lock()
        self._load()

    def set_briefing_resolver(self, fn: Optional[Callable[[str], str]]) -> None:
        """注入社交简报生成器（与能力画像一样走注入，不 import relations 之外的层）。"""
        self._briefing_resolver = fn

    # ------------------------------------------------------------------ #
    # 注册 / 发现
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        for spec in self.hub_config.agents:
            try:
                adapter = build_adapter(spec, self.settings)
            except Exception as exc:  # noqa: BLE001
                log.error("加载 agent `%s` 失败：%s", spec.id, exc)
                continue
            self._records[spec.id] = AgentRecord(spec, adapter)
        log.info("已注册 %d 个 agent：%s", len(self._records), ", ".join(self._records))

    def list_records(self) -> list[AgentRecord]:
        return list(self._records.values())

    def get(self, agent_id: str) -> AgentRecord:
        rec = self._records.get(agent_id)
        if rec is None:
            raise A2AError(
                JsonRpcErrorCodes.INVALID_PARAMS,
                f"未找到 agent `{agent_id}`",
                {"available": list(self._records)},
            )
        return rec

    def has(self, agent_id: str) -> bool:
        return agent_id in self._records

    def snapshot(self, base_url: Optional[str] = None) -> list[dict[str, Any]]:
        base = base_url or self.settings.public_url
        return [r.snapshot(base) for r in self._records.values()]

    def hub_card(self, social: Any = None) -> dict[str, Any]:
        """Hub 自身的 Agent Card —— 聚合所有子 agent 的 skill。

        ``social`` 传入关系图时，如果好友门禁开着，就在卡里声明
        ``x-social`` 扩展。**这一步不是可选的**：不声明的话，标准 A2A 客户端
        收到「非好友」的拒绝会以为对方坏了，而不是「我该先去加好友」。
        """
        base = self.settings.public_url.rstrip("/")
        skills: list[dict[str, Any]] = [
            {
                "id": "multi-agent-collaboration",
                "name": "多 Agent 协同编排",
                "description": (
                    "以 delegate / broadcast / pipeline / roundtable 四种模式，"
                    "调度 WorkBuddy、千问办公、扣子、Codex、Claude Code 等异构 agent 协同完成任务"
                ),
                "tags": ["orchestration", "multi-agent", "collaboration", "a2a"],
                "examples": [
                    "让 Codex 写代码，Claude Code 做代码评审",
                    "并行问三个模型同一个问题并汇总对比",
                    "先调研再写作再润色的三级流水线",
                ],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain"],
            },
            {
                "id": "agent-discovery",
                "name": "Agent 能力发现",
                "description": "发布/拉取 Agent Card，按技能标签与关键词自动路由到最合适的 agent",
                "tags": ["discovery", "routing", "registry"],
                "examples": ["有哪些 agent 能做代码评审？"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
        ]
        for r in self._records.values():
            for s in r.adapter.skills:
                d = s.model_dump()
                d["id"] = f"{r.id}:{s.id}"
                skills.append(d)

        card: dict[str, Any] = {
            "name": self.settings.hub_name,
            "description": (
                "异构 AI Agent 互联互通网关。将 WorkBuddy / 千问办公 / 扣子 Coze / "
                "Codex / Claude Code 等统一封装为符合 A2A 协议的 agent，"
                "提供能力发现、任务委派、流式回传与多智能体协同编排。"
            ),
            "url": f"{base}/",
            "version": APP_VERSION,
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "additionalInterfaces": [{"url": f"{base}/", "transport": "JSONRPC"}],
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": True,
            },
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "skills": skills,
            "provider": {"organization": "A2A Hub", "url": base},
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer"},
            }
            if self.settings.api_token
            else {},
            "security": [{"bearer": []}] if self.settings.api_token else [],
            "metadata": {
                "agentCount": len(self._records),
                "agents": [r.id for r in self._records.values()],
                "collaborationModes": ["delegate", "broadcast", "pipeline", "roundtable"],
            },
        }

        # 门禁开着时必须自我声明，否则标准客户端会把 -32008 当成故障
        if social is not None and getattr(social, "enabled", False):
            mode = getattr(social, "mode", "strict")
            card["extensions"] = [
                {
                    "uri": "https://a2a-hub.local/x-social",
                    "required": False,
                    "description": (
                        "本 Hub 启用了好友制访问控制。非好友调用会返回 JSON-RPC "
                        "-32008，并附带 data.hint 指明如何发起好友申请。"
                    ),
                }
            ]
            # 用 update 而不是再写一个 "metadata" 键——后者会把 agent 列表整段盖掉
            card["metadata"]["social"] = {"enabled": True, "mode": mode}
            card["description"] += (
                f"（已启用社交门禁，模式 {mode}：需先与目标成员建立好友关系）"
            )
        return card

    def agent_card(self, agent_id: str) -> dict[str, Any]:
        rec = self.get(agent_id)
        return rec.adapter.card(self.settings.public_url)

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    async def check_health(self, force: bool = False, ttl: float = 60.0) -> dict[str, Any]:
        now = time.time()
        results: dict[str, Any] = {}

        async def probe(rec: AgentRecord) -> tuple[str, dict[str, Any]]:
            if not force and now - rec.checked_at < ttl:
                return rec.id, rec.health
            try:
                h = await asyncio.wait_for(rec.adapter.health(), timeout=20)
            except asyncio.TimeoutError:
                h = {"status": "degraded", "detail": "健康检查超时"}
            except Exception as exc:  # noqa: BLE001
                h = {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}
            rec.health = h
            rec.checked_at = time.time()
            return rec.id, h

        if self._records:
            for aid, h in await asyncio.gather(*(probe(r) for r in self._records.values())):
                results[aid] = h
        return results

    # ------------------------------------------------------------------ #
    # 能力路由
    # ------------------------------------------------------------------ #

    def score(self, rec: AgentRecord, query: str) -> float:
        """粗略但够用的相关性打分：skill 关键词命中 + 标签 + 优先级。

        不是要取代语义检索，而是让"该找谁"这件事在本地零成本决定。
        真正的语义路由可以把这里换成向量检索——接口不变。
        """
        tokens = _tokenize(query)
        if not tokens:
            return float(rec.spec.priority)

        score = float(rec.spec.priority)
        haystacks: list[tuple[str, float]] = []
        for s in rec.adapter.skills:
            haystacks.append((s.name, 2.0))
            haystacks.append((s.description, 1.5))
            haystacks.extend((t, 2.5) for t in s.tags)
            haystacks.append((s.id.replace("-", " "), 1.5))
        haystacks.append((rec.spec.description, 1.0))
        haystacks.append((rec.spec.name, 1.0))
        haystacks.extend((t, 1.2) for t in rec.spec.tags)

        for text, weight in haystacks:
            low = text.lower()
            for tok in tokens:
                if tok and tok in low:
                    score += weight * (1.0 + min(len(tok), 8) / 8.0)
        return score

    def route(self, query: str, exclude: Optional[set[str]] = None) -> Optional[AgentRecord]:
        """选一个最合适的可用 agent。"""
        exclude = exclude or set()
        best: Optional[AgentRecord] = None
        best_score = float("-inf")
        for rec in self._records.values():
            if rec.id in exclude or not rec.spec.enabled or not rec.spec.auto_route:
                continue
            if rec.health.get("status") == "unavailable":
                continue
            s = self.score(rec, query)
            if s > best_score:
                best, best_score = rec, s
        return best

    def rank(
        self, query: str, top_k: int = 5, only_enabled: bool = True
    ) -> list[tuple[AgentRecord, float]]:
        scored = [
            (r, self.score(r, query))
            for r in self._records.values()
            if (r.spec.enabled or not only_enabled) and r.spec.auto_route
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------ #
    # 任务执行
    # ------------------------------------------------------------------ #

    def new_task(
        self,
        agent_id: str,
        message: Message,
        context_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Task:
        task = Task(
            contextId=context_id or message.contextId or Task().contextId,
            metadata=metadata or {},
        )
        task.agentId = agent_id
        message.taskId = task.id
        message.contextId = task.contextId
        task.history.append(message)
        return task

    async def execute(
        self, record: AgentRecord, task: Task, message: Message
    ) -> AsyncIterator[TaskEvent]:
        """驱动适配器执行任务，逐事件落库 + 推总线 + 向上产出。"""
        briefing = ""
        if self._briefing_resolver is not None:
            try:
                briefing = self._briefing_resolver(record.id) or ""
            except Exception:  # noqa: BLE001 - 简报失败绝不能拖垮任务本身
                log.debug("社交简报生成失败 agent=%s", record.id, exc_info=True)
                briefing = ""
        ctx = TaskContext(
            task=task, message=message, adapter=record.adapter, bus=self.bus,
            briefing=briefing,
        )
        record.task_count += 1

        self.store.save(task)
        self.bus.publish(task.id, task)
        self.bus.publish_global(
            {
                "kind": "hub-event",
                "event": "task-created",
                "taskId": task.id,
                "agentId": record.id,
                "task": task.model_dump(mode="json"),
            }
        )

        try:
            async for event in record.adapter.run(ctx):
                self.store.save(task)
                self.bus.publish(task.id, event)
                self.bus.publish_global(
                    {
                        "kind": "hub-event",
                        "event": "task-progress",
                        "taskId": task.id,
                        "agentId": record.id,
                        "state": task.status.state.value,
                        "event": event.model_dump(mode="json"),
                    }
                )
                yield event
        except asyncio.CancelledError:
            if not task.status.is_terminal:
                task.touch(TaskState.CANCELED, Message.agent("任务被中断"))
            self.store.save(task)
            raise
        except Exception as exc:  # noqa: BLE001
            record.error_count += 1
            log.exception("agent `%s` 执行任务 %s 失败", record.id, task.id)
            task.touch(TaskState.FAILED, Message.agent(f"执行异常：{exc}"))
            self.store.save(task)
            raise
        finally:
            if task.status.state in (TaskState.FAILED, TaskState.CANCELED):
                record.error_count += 1
            self.store.save(task)
            self.bus.close_task(task.id)
            self.bus.publish_global(
                {
                    "kind": "hub-event",
                    "event": "task-finished",
                    "taskId": task.id,
                    "agentId": record.id,
                    "state": task.status.state.value,
                }
            )

    async def run_blocking(
        self,
        agent_id: str,
        prompt: str,
        context_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Task:
        """便捷方法：执行完整个任务并返回最终 Task（供编排器内部使用）。"""
        rec = self.get(agent_id)
        message = Message.user(prompt, contextId=context_id)
        task = self.new_task(agent_id, message, context_id, metadata)
        limit = timeout or self.settings.task_timeout

        async def drain() -> None:
            async for _ in self.execute(rec, task, message):
                pass

        try:
            await asyncio.wait_for(drain(), timeout=limit)
        except asyncio.TimeoutError:
            await rec.adapter.cancel(task.id)
            task.touch(TaskState.FAILED, Message.agent(f"任务超时（>{int(limit)}s）"))
            self.store.save(task)
            self.bus.close_task(task.id)
        return task

    async def cancel(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise A2AError(JsonRpcErrorCodes.TASK_NOT_FOUND, f"未找到任务 {task_id}")
        if task.status.is_terminal:
            raise A2AError(
                JsonRpcErrorCodes.TASK_NOT_CANCELABLE,
                f"任务已处于终态 `{task.status.state.value}`，无法取消",
            )
        if task.agentId and self.has(task.agentId):
            await self._records[task.agentId].adapter.cancel(task_id)
        task.touch(TaskState.CANCELED, Message.agent("任务已被调用方取消"))
        self.store.save(task)
        self.bus.publish(task_id, task)
        self.bus.close_task(task_id)
        return task

    async def aclose(self) -> None:
        for rec in self._records.values():
            await rec.adapter.aclose()


# --------------------------------------------------------------------------- #
# 单例装配
# --------------------------------------------------------------------------- #

_registry: Optional[AgentRegistry] = None


def bootstrap(settings: Optional[Settings] = None) -> AgentRegistry:
    """构建全局 registry（幂等）。"""
    global _registry
    if _registry is not None:
        return _registry
    from .store import build_store

    s = settings or Settings()
    cfg = HubConfig.load(s.agents_path())
    store = build_store(s.store, s.db_path)
    bus = EventBus()
    _registry = AgentRegistry(s, cfg, store, bus)
    return _registry


def get_registry() -> AgentRegistry:
    return bootstrap()
