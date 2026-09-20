"""事件总线 —— 为 SSE 流式回传提供按 taskId 订阅的异步广播。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator, Optional

from .models import (
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    new_id,
)


class EventBus:
    """极简 pub/sub。

    每个 taskId 一个订阅者集合；同时维护一个全局频道，供 Web 控制台
    观察所有 agent 的活动（跨任务），用于画协同拓扑。
    """

    def __init__(self, queue_size: int = 512) -> None:
        self._subs: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)
        self._global: set[asyncio.Queue[Any]] = set()
        self._queue_size = queue_size

    def _make_queue(self) -> asyncio.Queue[Any]:
        return asyncio.Queue(maxsize=self._queue_size)

    # --------------------------- 订阅 --------------------------- #

    def subscribe(self, task_id: str) -> asyncio.Queue[Any]:
        q = self._make_queue()
        self._subs[task_id].add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue[Any]) -> None:
        self._subs[task_id].discard(q)
        if not self._subs[task_id]:
            self._subs.pop(task_id, None)

    def subscribe_global(self) -> asyncio.Queue[Any]:
        q = self._make_queue()
        self._global.add(q)
        return q

    def unsubscribe_global(self, q: asyncio.Queue[Any]) -> None:
        self._global.discard(q)

    # --------------------------- 发布 --------------------------- #

    def publish(self, task_id: str, event: Any) -> None:
        """非阻塞投递；队列满时丢弃最旧事件，避免拖垮生产者。

        注意这里必须先转成 set 再做并集——``list | set`` 会直接抛 TypeError。
        """
        targets = set(self._subs.get(task_id, ())) | self._global
        for q in targets:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish_global(self, event: Any) -> None:
        for q in list(self._global):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close_task(self, task_id: str) -> None:
        """发送终止哨兵，让所有订阅者退出 async for。"""
        for q in list(self._subs.get(task_id, ())):
            try:
                q.put_nowait(STREAM_END)
            except asyncio.QueueFull:
                pass

    # --------------------------- 消费 --------------------------- #

    async def stream(self, task_id: str, timeout: float = 0.0) -> AsyncIterator[Any]:
        """订阅并产出事件。

        ``timeout`` 为 0 表示一直等；大于 0 时**空闲超时即结束流**，
        适合"重连一次看一批"的场景。需要长连接请自行 subscribe + 心跳，
        见 ``next_event``。
        """
        q = self.subscribe(task_id)
        try:
            while True:
                got, item = await self.next_event(q, timeout)
                if not got:
                    return
                if item is STREAM_END:
                    return
                yield item
        finally:
            self.unsubscribe(task_id, q)

    @staticmethod
    async def next_event(q: asyncio.Queue[Any], timeout: float) -> tuple[bool, Any]:
        """等待下一条事件。

        返回 ``(是否拿到, 事件)``；超时返回 ``(False, None)``。
        asyncio.Queue.get 被取消时不会丢消息（元素只在 get_nowait 里出队），
        所以"超时后再来一轮"是安全的，不会漏事件。
        """
        try:
            if timeout and timeout > 0:
                item = await asyncio.wait_for(q.get(), timeout=timeout)
            else:
                item = await q.get()
        except asyncio.TimeoutError:
            return False, None
        return True, item


class _Sentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<STREAM_END>"


#: 流结束哨兵，订阅者收到它即应停止迭代
STREAM_END = _Sentinel()


# --------------------------------------------------------------------------- #
# 事件构造快捷函数
# --------------------------------------------------------------------------- #


def status_event(task: Task, final: bool = False) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        taskId=task.id, contextId=task.contextId, status=task.status, final=final
    )


def artifact_event(
    task: Task, artifact_id: str, append: bool = False, last_chunk: bool = False
) -> Optional[TaskArtifactUpdateEvent]:
    for a in task.artifacts:
        if a.artifactId == artifact_id:
            return TaskArtifactUpdateEvent(
                taskId=task.id,
                contextId=task.contextId,
                artifact=a,
                append=append,
                lastChunk=last_chunk,
            )
    return None


__all__ = [
    "EventBus",
    "STREAM_END",
    "status_event",
    "artifact_event",
    "new_id",
]
