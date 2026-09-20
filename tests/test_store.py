"""存储层测试。"""

from __future__ import annotations

import pytest

from a2a_hub.models import Artifact, Task, TaskState, TextPart
from a2a_hub.store import MemoryTaskStore, SqliteTaskStore, build_store


def make_task(agent_id: str = "echo-a", text: str = "内容") -> Task:
    t = Task(agentId=agent_id)
    t.add_artifact(Artifact(name="output", parts=[TextPart(text=text)]))
    t.touch(TaskState.COMPLETED)
    return t


@pytest.mark.parametrize("factory", [
    lambda tmp: MemoryTaskStore(),
    lambda tmp: SqliteTaskStore(str(tmp / "t.db")),
])
class TestStoreContract:
    def test_save_and_get(self, factory, tmp_path):
        store = factory(tmp_path)
        t = make_task()
        store.save(t)
        got = store.get(t.id)
        assert got is not None
        assert got.id == t.id
        assert got.agentId == "echo-a"
        assert got.status.state == TaskState.COMPLETED
        assert got.final_text() == "内容"

    def test_get_missing_returns_none(self, factory, tmp_path):
        assert factory(tmp_path).get("nope") is None

    def test_update_overwrites(self, factory, tmp_path):
        store = factory(tmp_path)
        t = make_task()
        store.save(t)
        t.add_artifact(Artifact(name="output", parts=[TextPart(text="追加")]))
        store.save(t)
        assert store.get(t.id).final_text() == "内容\n追加"

    def test_list_newest_first(self, factory, tmp_path):
        store = factory(tmp_path)
        ids = []
        for i in range(3):
            t = make_task(text=f"第{i}条")
            store.save(t)
            ids.append(t.id)
        listed = [x.id for x in store.list(limit=10)]
        assert listed[0] == ids[-1]

    def test_list_respects_limit(self, factory, tmp_path):
        store = factory(tmp_path)
        for i in range(10):
            store.save(make_task(text=f"x{i}"))
        assert len(store.list(limit=4)) == 4

    def test_delete(self, factory, tmp_path):
        store = factory(tmp_path)
        t = make_task()
        store.save(t)
        store.delete(t.id)
        assert store.get(t.id) is None


def test_memory_store_evicts_oldest():
    store = MemoryTaskStore(max_items=3)
    ids = []
    for i in range(5):
        t = make_task(text=f"n{i}")
        store.save(t)
        ids.append(t.id)
    assert store.get(ids[0]) is None       # 最早的被淘汰
    assert store.get(ids[-1]) is not None   # 最新的保留


def test_build_store_selects_backend(tmp_path):
    assert isinstance(build_store("memory", ""), MemoryTaskStore)
    assert isinstance(build_store("sqlite", str(tmp_path / "a.db")), SqliteTaskStore)
    assert isinstance(build_store("weird", ""), MemoryTaskStore)  # 未知类型降级


def test_sqlite_persists_across_instances(tmp_path):
    path = tmp_path / "persist.db"
    s1 = SqliteTaskStore(str(path))
    t = make_task(text="持久化")
    s1.save(t)
    s1.close()

    s2 = SqliteTaskStore(str(path))
    assert s2.get(t.id).final_text() == "持久化"
    s2.close()
