"""适配器基类与运行时工具。

设计要点
--------
异构 agent 的差异被压缩到唯一一个方法：``BaseAdapter.execute(ctx)``，
它是一个异步生成器，产出 A2A 事件（status-update / artifact-update）。
上层（registry / rpc / orchestrator）完全不感知底层是 CLI 子进程、
HTTP API 还是另一个 A2A 服务。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from ..bus import EventBus
from ..config import AgentSpec
from ..models import (
    AgentCapabilities,
    AgentSkill,
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
    new_id,
    utc_now,
)


class AdapterError(RuntimeError):
    """适配器执行失败（会映射为 Task 的 failed 状态）。"""


@dataclass
class TaskContext:
    """一次任务执行的上下文，同时提供事件构造助手工厂。"""

    task: Task
    message: Message
    adapter: "BaseAdapter"
    bus: Optional[EventBus] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # 流式 artifact 的累计缓冲：artifactName -> Artifact
    _artifacts: dict[str, Artifact] = field(default_factory=dict, init=False)

    @property
    def prompt(self) -> str:
        """调用方输入文本（多模态时取所有 part 的文本拼接）。"""
        return self.message.text()

    # --------------------------- 事件工坊 --------------------------- #

    def status(
        self, state: TaskState, text: Optional[str] = None, final: bool = False
    ) -> TaskStatusUpdateEvent:
        msg = Message.agent(text, taskId=self.task.id, contextId=self.task.contextId) if text else None
        self.task.touch(state, msg)
        if msg:
            self.task.history.append(msg)
        return TaskStatusUpdateEvent(
            taskId=self.task.id,
            contextId=self.task.contextId,
            status=self.task.status,
            final=final,
        )

    def artifact(
        self,
        text: str,
        name: str = "output",
        append: bool = False,
        last_chunk: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TaskArtifactUpdateEvent:
        art = self._artifacts.get(name)
        if art is None:
            art = Artifact(name=name, parts=[], metadata=metadata or {})
            self._artifacts[name] = art
            self.task.artifacts.append(art)
            append = False
        art.parts.append(TextPart(text=text))
        return TaskArtifactUpdateEvent(
            taskId=self.task.id,
            contextId=self.task.contextId,
            artifact=art,
            append=append,
            lastChunk=last_chunk,
        )

    def finish(self, text: Optional[str] = None) -> list[TaskEvent]:
        """正常收尾：把最后一段文本落成 artifact 并置 completed。"""
        events: list[TaskEvent] = []
        if text:
            events.append(self.artifact(text, name="output", append=True, last_chunk=True))
        events.append(self.status(TaskState.COMPLETED, final=True))
        return events

    def fail(self, error: str) -> list[TaskEvent]:
        return [self.status(TaskState.FAILED, f"执行失败：{error}", final=True)]


# --------------------------------------------------------------------------- #
# 子进程运行时
# --------------------------------------------------------------------------- #


class ProcessRegistry:
    """登记在跑的子进程，支持按 taskId 取消。"""

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def register(self, task_id: str, proc: asyncio.subprocess.Process) -> None:
        self._procs[task_id] = proc

    def unregister(self, task_id: str) -> None:
        self._procs.pop(task_id, None)

    def is_running(self, task_id: str) -> bool:
        proc = self._procs.get(task_id)
        return proc is not None and proc.returncode is None

    async def kill(self, task_id: str) -> bool:
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return False
        await _terminate(proc)
        return True

    async def kill_all(self) -> None:
        for task_id in list(self._procs):
            await self.kill(task_id)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """跨平台终止：Windows 用 taskkill 连子进程树一起收，POSIX 用进程组。"""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, FileNotFoundError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def resolve_executable(name_or_path: str) -> Optional[str]:
    """在 PATH 里找可执行文件；Windows 上补齐 .cmd/.exe 后缀。

    npm 全局安装的 CLI（claude / codex）在 Windows 下是 .cmd，
    asyncio 用 shell=False 直接起 .cmd 会失败，这里显式解析。
    """
    if not name_or_path:
        return None
    p = shutil.which(name_or_path)
    if p:
        return p
    if sys.platform == "win32":
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            p = shutil.which(name_or_path + ext)
            if p:
                return p
    return None


async def run_command_stream(
    argv: list[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    stdin_text: Optional[str] = None,
    timeout: float = 180.0,
    task_id: str = "",
    registry: Optional[ProcessRegistry] = None,
    shell: bool = False,
) -> AsyncIterator[str]:
    """执行命令并逐行流式产出 stdout。

    统一处理：编码（Windows 下强制 utf-8 + errors=replace）、超时、
    取消、子进程树回收。
    """
    merged_env = {**os.environ, **(env or {})}
    merged_env.setdefault("PYTHONIOENCODING", "utf-8")

    kwargs: dict[str, Any] = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.PIPE if stdin_text else asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=merged_env,
        limit=1024 * 1024,
    )
    if sys.platform != "win32" and not shell:
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except FileNotFoundError as exc:
        raise AdapterError(f"找不到可执行文件：{argv[0]}") from exc

    if registry and task_id:
        registry.register(task_id, proc)

    async def _feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_text.encode("utf-8", "replace"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # pragma: no cover - best effort
                pass

    feeder = asyncio.create_task(_feed_stdin()) if stdin_text else None

    try:
        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                await _terminate(proc)
                raise AdapterError(f"执行超时（>{int(timeout)}s）") from exc
            if not line:
                break
            yield line.decode("utf-8", "replace").rstrip("\r\n")
        code = await proc.wait()
        if code != 0:
            raise AdapterError(f"命令退出码 {code}")
    finally:
        if feeder:
            feeder.cancel()
        if registry and task_id:
            registry.unregister(task_id)


def split_command(cmd: str) -> list[str]:
    """按平台正确切分命令模板字符串。"""
    return shlex.split(cmd, posix=(sys.platform != "win32"))


# --------------------------------------------------------------------------- #
# 适配器基类
# --------------------------------------------------------------------------- #


class BaseAdapter(ABC):
    """所有异构 agent 的统一门面。"""

    #: 适配器类型标识，与 agents.yaml 的 type 字段对应
    type: str = "base"
    #: 是否支持流式增量输出
    streaming: bool = False

    def __init__(self, spec: AgentSpec, hub_settings: Any = None) -> None:
        self.spec = spec
        self.settings = hub_settings
        self.processes = ProcessRegistry()
        self._cancelled: set[str] = set()

    # --------------------------- 元信息 --------------------------- #

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def description(self) -> str:
        return self.spec.description

    @property
    def skills(self) -> list[AgentSkill]:
        out: list[AgentSkill] = []
        for s in self.spec.skills:
            data = dict(s)
            if "id" not in data:
                data["id"] = data.get("name", "skill").lower().replace(" ", "-")
            if "name" not in data:
                data["name"] = data["id"]
            out.append(AgentSkill(**data))
        return out

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(streaming=self.streaming)

    def card(self, base_url: str) -> dict[str, Any]:
        """生成该 agent 的 A2A Agent Card。"""
        return {
            "name": self.name,
            "description": self.description,
            "url": f"{base_url.rstrip('/')}/agents/{self.id}/",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "additionalInterfaces": [
                {"url": f"{base_url.rstrip('/')}/agents/{self.id}/", "transport": "JSONRPC"}
            ],
            "capabilities": self.capabilities().model_dump(),
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain"],
            "skills": [s.model_dump() for s in self.skills],
            "provider": {"organization": "A2A Hub", "url": base_url.rstrip("/")},
            "metadata": {
                "adapterType": self.type,
                "tags": self.spec.tags,
                "hubAgentId": self.id,
            },
        }

    # --------------------------- 生命周期 --------------------------- #

    @abstractmethod
    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        """执行任务，产出 A2A 事件流。子类必须实现。"""
        raise NotImplementedError
        yield  # pragma: no cover - 让类型检查识别为异步生成器

    async def health(self) -> dict[str, Any]:
        """默认健康检查：构造成功即视为健康，具体适配器可覆盖。"""
        return {"status": "healthy" if self.spec.enabled else "disabled", "detail": ""}

    async def cancel(self, task_id: str) -> bool:
        self._cancelled.add(task_id)
        return await self.processes.kill(task_id)

    async def aclose(self) -> None:
        await self.processes.kill_all()

    # --------------------------- 便捷包装 --------------------------- #

    async def run(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        """带标准状态机（submitted -> working -> ...）的执行包装。

        刻意避免在 ``finally`` 里 ``yield``：异步生成器在关闭时如果从 finally
        再产出事件会抛 RuntimeError（yield inside finally during aclose）。
        """
        yield ctx.status(TaskState.WORKING, "任务已受理，开始执行")

        error: Optional[str] = None
        canceled = False

        try:
            async for event in self.execute(ctx):
                if ctx.task.id in self._cancelled:
                    canceled = True
                    break
                yield event
        except AdapterError as exc:
            error = str(exc)
        except asyncio.CancelledError:
            await self.cancel(ctx.task.id)
            canceled = True
            raise
        except GeneratorExit:  # pragma: no cover - 调用方提前关闭
            await self.cancel(ctx.task.id)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免异常吞掉任务
            error = f"{type(exc).__name__}: {exc}"

        if canceled:
            yield ctx.status(TaskState.CANCELED, "任务已被取消", final=True)
            return
        if error is not None:
            for e in ctx.fail(error):
                yield e
            return
        # 适配器若已自行置为终态或 input-required，则尊重它的判断，不再覆盖
        if ctx.task.status.state in (TaskState.SUBMITTED, TaskState.WORKING):
            yield ctx.status(TaskState.COMPLETED, final=True)


__all__ = [
    "BaseAdapter",
    "TaskContext",
    "AdapterError",
    "ProcessRegistry",
    "run_command_stream",
    "resolve_executable",
    "split_command",
    "new_id",
    "utc_now",
]
