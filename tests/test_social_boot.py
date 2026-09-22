"""社交层一键启用（``social init`` / ``a2a_social_init``）的回归。

守的是这几件事：
1. 一键生成出来的配置**真能启用门禁**（不是生成一堆坏 YAML）；
2. **绝不覆盖**用户手改过的 members.yaml；
3. **绝不写 token** —— 示例模板里那句 ``tokens: ["${VAR}"]`` 在变量没设时会
   把部署者锁在门外（403 与 401 齐飞），一键生成必须避开；
4. :meth:`SocialGraph.reload_members` 是**就地**改，所以「不重启就能启用」
   这件事不会被后来的重构悄悄破坏（引用一换，五个持有方就状态分裂）；
5. agent 的 bio 是从 ``agents.yaml`` 抄来的自由文本，可能带引号/冒号，
   渲染必须仍然生成合法 YAML。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_hub.relations import SocialGraph  # noqa: E402
from a2a_hub.social_boot import (  # noqa: E402
    DEFAULT_OWNER_ID,
    bootstrap_members,
    load_agent_declarations,
    render_members_yaml,
)


AGENTS_YAML = """
agents:
  - id: echo
    name: Echo
    description: 回声，用来做冒烟
  - id: weird
    name: Weird
    description: '带: 冒号与 "引号" 的诡异描述'
"""


@pytest.fixture()
def agents_file(tmp_path: Path) -> Path:
    p = tmp_path / "agents.yaml"
    p.write_text(AGENTS_YAML, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


class TestBootstrap:
    def test_creates_usable_members_file(self, tmp_path: Path, agents_file: Path):
        mp = tmp_path / "members.yaml"
        info = bootstrap_members(members_path=mp, agents_path=agents_file)

        assert info["created"] is True
        assert mp.exists()

        members = SocialGraph.load_members(mp)
        graph = SocialGraph(members, mode="strict", path=tmp_path / "rel.json")
        # 最关键的一条：生成的东西真能把门禁打开
        assert graph.enabled is True
        ids = {m.id for m in members}
        assert DEFAULT_OWNER_ID in ids
        assert "agent:echo" in ids
        assert "agent:weird" in ids

    def test_owner_declared_so_delegate_works(self, tmp_path: Path, agents_file: Path):
        """不写 owner 时单人部署会回落，但多人类部署 ``effective_owner`` 拒绝猜。

        一键生成必须落在「一定能用」的一边——显式声明 owner。
        """
        mp = tmp_path / "members.yaml"
        bootstrap_members(members_path=mp, agents_path=agents_file)
        members = {m.id: m for m in SocialGraph.load_members(mp)}
        assert members["agent:echo"].owner == DEFAULT_OWNER_ID

    def test_no_tokens_so_nobody_gets_locked_out(self, tmp_path: Path, agents_file: Path):
        mp = tmp_path / "members.yaml"
        bootstrap_members(members_path=mp, agents_path=agents_file)
        graph = SocialGraph(SocialGraph.load_members(mp), mode="strict",
                            path=tmp_path / "rel.json")
        # 有成员但没 token：按本地开发模式放行，不会 401/403 齐飞
        assert graph.enabled is True
        assert graph.any_tokens() is False

    def test_idempotent_and_never_overwrites(self, tmp_path: Path, agents_file: Path):
        mp = tmp_path / "members.yaml"
        mp.write_text("members:\n- id: human:mine\n  name: 我手改的\n  kind: human\n",
                      encoding="utf-8")

        info = bootstrap_members(members_path=mp, agents_path=agents_file)
        assert info["created"] is False
        # 内容必须原封不动
        assert "我手改的" in mp.read_text(encoding="utf-8")
        assert info["members"] == ["human:mine"]

    def test_missing_agents_file_still_works(self, tmp_path: Path):
        """读不到 agents.yaml 不该让一键启用失败——只是通讯录空一点。"""
        mp = tmp_path / "members.yaml"
        info = bootstrap_members(members_path=mp, agents_path=tmp_path / "nope.yaml")
        assert info["created"] is True
        assert SocialGraph.load_members(mp)  # 至少有人类


class TestRendering:
    def test_bio_with_quotes_and_colons_stays_valid_yaml(self, agents_file: Path):
        agents = load_agent_declarations(agents_file)
        text = render_members_yaml(agents)
        parsed = yaml.safe_load(text)  # 不该抛
        weird = [m for m in parsed["members"] if m["id"] == "agent:weird"]
        assert weird and "冒号" in weird[0]["bio"]

    def test_no_agents_file_returns_empty(self, tmp_path: Path):
        assert load_agent_declarations(tmp_path / "nope.yaml") == []


# --------------------------------------------------------------------------- #
# 就地重载 ——「不重启」的实现基础
# --------------------------------------------------------------------------- #


class TestReloadMembers:
    def _graph(self, tmp_path: Path, members=None) -> SocialGraph:
        return SocialGraph(members or [], mode="strict", path=tmp_path / "rel.json")

    def test_reload_flips_enabled_without_new_object(self, tmp_path: Path,
                                                     agents_file: Path):
        mp = tmp_path / "members.yaml"
        bootstrap_members(members_path=mp, agents_path=agents_file)

        g = self._graph(tmp_path)
        assert g.enabled is False
        ref = g  # 持有方拿的就是这个对象

        g.reload_members(SocialGraph.load_members(mp))
        assert g.enabled is True
        assert ref is g, "重建对象会让五个持有方状态分裂"

    def test_reload_drops_empty_tokens(self, tmp_path: Path):
        """``${VAR}`` 没设时 ``expand_env`` 会展开成空串，空串必须丢掉。

        留着它会让 ``any_tokens()`` 误判成「配了 token」，于是既 403 又 401，
        把部署者锁在门外。（把字面量 ``${VAR}`` 剥掉是 ``expand_env`` 的职责，
        不在 reload 这里做——两者各管一段，别重复也别漏。）
        """
        from a2a_hub.relations import Member

        g = self._graph(tmp_path)
        g.reload_members([Member(id="human:default", name="我", kind="human",
                                 tokens=["", "   "])])
        assert g.any_tokens() is False

    def test_real_file_has_no_unexpanded_placeholders(self, tmp_path: Path,
                                                      agents_file: Path):
        """端到端兜底：一键生成 → load_members，全程不出现空/占位 token。"""
        mp = tmp_path / "members.yaml"
        bootstrap_members(members_path=mp, agents_path=agents_file)
        g = self._graph(tmp_path)
        g.reload_members(SocialGraph.load_members(mp))
        assert g.enabled is True
        assert g.any_tokens() is False

    def test_reload_keeps_existing_members(self, tmp_path: Path):
        from a2a_hub.relations import Member

        g = self._graph(tmp_path, [Member(id="human:old", name="旧人", kind="human")])
        g.reload_members([Member(id="human:default", name="新人", kind="human")])
        ids = {m.id for m in g.all_members()}
        # 不删旧的：已建立的关系不能悬空
        assert "human:old" in ids and "human:default" in ids

    def test_mode_off_stays_disabled(self, tmp_path: Path, agents_file: Path):
        mp = tmp_path / "members.yaml"
        bootstrap_members(members_path=mp, agents_path=agents_file)
        g = SocialGraph([], mode="off", path=tmp_path / "rel.json")
        g.reload_members(SocialGraph.load_members(mp))
        assert g.enabled is False
