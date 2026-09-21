"""社交图谱（关系层）测试。

这一层是「Hub = 社交网络」这条设计的落点，所以测试死盯四条不变量：

1. **聊天权 ≠ 指挥权。** 成为好友只默认给 ``peek/chat/invite``；
   ``delegate`` 必须显式授予。这是整套权限设计里最重要的默认值。
2. **权限上行闭包。** ``grant(A→B) ⊆ owned_scopes(A)``，否则跟 A 交上朋友
   就能绕道拿到 A 的 owner 的资源（社交网络最典型的提权路径）。
3. **群聊只放宽 chat。** 建个群不能变成绕过好友制去驱动别人 agent 的后门。
4. **一条边双向成立。** ``pair`` 规范化成字典序，杜绝 (A,B)/(B,A) 两份记录。

再加上退化行为：**没有 members.yaml 时门禁整体关闭**，行为与 v0.3.0 一致；
以及阶段 1 顺带补掉的四个安全缺口（SSE 无鉴权 / mark_read 可伪造 reader /
/console 裸奔 / 单 token 无身份无审计）。
"""

from __future__ import annotations

import json

import pytest
import yaml

from a2a_hub.relations import (
    DEFAULT_FRIEND_SCOPES,
    DEFAULT_MEMBER,
    GROUP_SCOPES,
    Member,
    NullGuard,
    RelationState,
    Scope,
    SocialConflict,
    SocialError,
    SocialGraph,
    SocialNotPermitted,
    SYSTEM,
    USER,
)

HUMAN = "human:seafish"
GUEST = "human:guest"
CODEX = "agent:codex"
QWEN = "agent:qwen-office"
ECHO_A = "agent:echo-a"
ECHO_B = "agent:echo-b"

ALL_SCOPES = {s.value for s in Scope}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def make_members() -> list[Member]:
    """两个人类 + 两个自己的 agent —— 多租户场景，最能暴露归属问题。"""
    return [
        Member(id=HUMAN, name="海鱼", kind="human", discoverable="circle"),
        Member(id=GUEST, name="访客", kind="human", discoverable="public"),
        Member(id=CODEX, name="Codex", kind="agent", owner=HUMAN, discoverable="public",
               bio="算法与调试"),
        Member(id=QWEN, name="千问办公", kind="agent", owner=HUMAN, discoverable="private"),
    ]


@pytest.fixture
def graph(tmp_path) -> SocialGraph:
    """多人类 + 带归档路径的图谱（省得测持久化时再造一个）。"""
    return SocialGraph(make_members(), mode="strict", path=tmp_path / "relations.json")


@pytest.fixture
def solo() -> SocialGraph:
    """单人自用：只有一个人 + 两个没声明 owner 的 agent。"""
    return SocialGraph(
        [
            Member(id=HUMAN, name="海鱼", kind="human"),
            Member(id=ECHO_A, name="回显 A", kind="agent", discoverable="public"),
            Member(id=ECHO_B, name="回显 B", kind="agent", discoverable="public"),
        ],
        mode="strict",
    )


def befriend(g: SocialGraph, a: str, b: str, scopes=None) -> None:
    """`a` 申请、`b` 同意。"""
    g.request(a, b, "想和你建立联系")
    g.accept(b, a, scopes)


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #


class TestNormalize:
    def test_bare_agent_id_gets_prefix(self, graph: SocialGraph):
        assert graph.normalize("codex") == CODEX

    def test_known_prefix_is_kept(self, graph: SocialGraph):
        assert graph.normalize("agent:codex") == CODEX
        assert graph.normalize("human:seafish") == HUMAN
        assert graph.normalize("svc:billing") == "svc:billing"

    def test_user_maps_to_default_member(self, graph: SocialGraph):
        # 两人以上且没有显式 human:default → 不做猜测，落到 human:default
        assert graph.default_member_id == DEFAULT_MEMBER
        assert graph.normalize(USER) == DEFAULT_MEMBER

    def test_user_maps_to_the_only_human(self, solo: SocialGraph):
        # 单人自用是最常见形态：旧的 sender="user" 路径必须不用改
        assert solo.default_member_id == HUMAN
        assert solo.normalize(USER) == HUMAN

    def test_system_is_never_a_normal_human(self, graph: SocialGraph):
        assert graph.normalize(SYSTEM) == SYSTEM

    def test_empty_ref_falls_back_to_default_member(self, graph: SocialGraph):
        assert graph.normalize("") == DEFAULT_MEMBER

    def test_agent_id_of(self):
        assert SocialGraph.agent_id_of(CODEX) == "codex"
        assert SocialGraph.agent_id_of(HUMAN) is None


# --------------------------------------------------------------------------- #
# 边存储：pair 规范化
# --------------------------------------------------------------------------- #


class TestPairNormalization:
    def test_one_edge_per_pair_regardless_of_direction(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "加个好友")
        a = graph.relation(HUMAN, CODEX)
        b = graph.relation(CODEX, HUMAN)
        assert a.pair == b.pair == tuple(sorted((HUMAN, CODEX)))

    def test_relation_is_sorted_lexicographically(self, graph: SocialGraph):
        graph.request(CODEX, HUMAN, "加个好友")
        assert graph.relation(HUMAN, CODEX).pair == (CODEX, HUMAN)

    def test_no_ghost_second_record(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "加个好友")
        graph.request(CODEX, HUMAN, "加个好友")  # 双向 → 直接成好友，但仍是一条边
        assert len(graph._rels) == 1

    def test_other_returns_the_counterpart(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "加个好友")
        rel = graph.relation(HUMAN, CODEX)
        assert rel.other(HUMAN) == CODEX
        assert rel.other(CODEX) == HUMAN
        assert rel.has(HUMAN) and rel.has(CODEX)


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


class TestStateMachine:
    def test_request_creates_pending(self, graph: SocialGraph):
        rel = graph.request(HUMAN, CODEX, "想用你的算法能力", [Scope.CHAT])
        assert rel.state is RelationState.PENDING
        assert rel.requestedBy == HUMAN
        assert rel.requestMessage == "想用你的算法能力"
        assert rel.requestedScopes == [Scope.CHAT]

    def test_request_requires_a_reason(self, graph: SocialGraph):
        with pytest.raises(SocialError):
            graph.request(HUMAN, CODEX, "   ")

    def test_cannot_request_self(self, graph: SocialGraph):
        with pytest.raises(SocialError):
            graph.request(HUMAN, HUMAN, "自言自语")

    def test_duplicate_request_conflicts(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "第一次")
        with pytest.raises(SocialConflict):
            graph.request(HUMAN, CODEX, "第二次")

    def test_accept_makes_them_friends(self, graph: SocialGraph):
        befriend(graph, HUMAN, CODEX)
        assert graph.is_friend(HUMAN, CODEX)
        assert graph.friends_of(HUMAN) == [CODEX]

    def test_cannot_accept_own_request(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        with pytest.raises(SocialConflict):
            graph.accept(HUMAN, CODEX)

    def test_accept_without_request_conflicts(self, graph: SocialGraph):
        with pytest.raises(SocialConflict):
            graph.accept(CODEX, HUMAN)

    def test_reject_then_cooldown_blocks_retry(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.reject(CODEX, HUMAN)
        assert graph.relation(HUMAN, CODEX).state is RelationState.REJECTED
        with pytest.raises(SocialConflict):
            graph.request(HUMAN, CODEX, "再试试")

    def test_cooldown_expiry_allows_retry(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.reject(CODEX, HUMAN)
        rel = graph.relation(HUMAN, CODEX)
        # 把时间拨回冷却期之外
        rel.updatedAt = "2000-01-01T00:00:00+00:00"
        again = graph.request(HUMAN, CODEX, "认真考虑过了")
        assert again.state is RelationState.PENDING

    def test_cancel_withdraws_my_request(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.cancel(HUMAN, CODEX)
        assert graph.relation(HUMAN, CODEX).state is RelationState.NONE
        assert graph.relation(HUMAN, CODEX).requestMessage == ""

    def test_cancel_only_works_for_my_own_request(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        with pytest.raises(SocialConflict):
            graph.cancel(CODEX, HUMAN)

    def test_revoke_clears_grants_and_keeps_the_record(self, graph: SocialGraph):
        befriend(graph, HUMAN, CODEX, [Scope.PEEK, Scope.CHAT, Scope.DELEGATE])
        graph.revoke(HUMAN, CODEX)
        rel = graph.relation(HUMAN, CODEX)
        assert rel.state is RelationState.NONE
        assert rel.grants == {}
        # 记录本身留着——审计链不能断
        assert rel.pair == tuple(sorted((HUMAN, CODEX)))

    def test_revoke_requires_friendship(self, graph: SocialGraph):
        with pytest.raises(SocialConflict):
            graph.revoke(HUMAN, CODEX)

    def test_block_is_one_way_and_silent(self, graph: SocialGraph):
        graph.block(CODEX, HUMAN)
        rel = graph.relation(CODEX, HUMAN)
        assert rel.state is RelationState.BLOCKED
        assert rel.blocked.get(CODEX) is True
        assert not rel.blocked.get(HUMAN)
        # 被拉黑的人再申请，只得到中性失败——不透露「你被拉黑了」
        with pytest.raises(SocialConflict) as err:
            graph.request(HUMAN, CODEX, "在吗")
        assert "拉黑" not in str(err.value)

    def test_block_kills_both_directions(self, graph: SocialGraph):
        befriend(graph, HUMAN, CODEX)
        graph.block(CODEX, HUMAN)
        assert graph.can(HUMAN, CODEX, Scope.CHAT) is False
        assert graph.can(CODEX, HUMAN, Scope.CHAT) is False

    def test_only_the_blocker_can_unblock(self, graph: SocialGraph):
        graph.block(CODEX, HUMAN)
        with pytest.raises(SocialConflict):
            graph.unblock(HUMAN, CODEX)
        graph.unblock(CODEX, HUMAN)
        assert graph.relation(CODEX, HUMAN).state is RelationState.NONE

    def test_cannot_block_self(self, graph: SocialGraph):
        with pytest.raises(SocialError):
            graph.block(HUMAN, HUMAN)


# --------------------------------------------------------------------------- #
# 双向同时申请 → 自动成为好友
# --------------------------------------------------------------------------- #


class TestMutualRequest:
    """用「访客 ↔ 别人的 agent」来测：owner 对自己的 agent 本来就全权，
    拿 owner 当被试会把「好友默认档」这条不变量掩盖掉。"""

    def test_simultaneous_requests_become_friends(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想加你")
        rel = graph.request(CODEX, GUEST, "我也想加你")
        assert rel.state is RelationState.FRIEND

    def test_mutual_friendship_grants_default_scopes_both_ways(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想加你")
        graph.request(CODEX, GUEST, "我也想加你")
        assert graph.can(GUEST, CODEX, Scope.CHAT)
        assert graph.can(CODEX, GUEST, Scope.CHAT)

    def test_mutual_friendship_never_auto_grants_delegate(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想加你")
        graph.request(CODEX, GUEST, "我也想加你")
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is False
        assert graph.can(CODEX, GUEST, Scope.DELEGATE) is False

    def test_request_when_already_friends_conflicts(self, graph: SocialGraph):
        befriend(graph, HUMAN, CODEX)
        with pytest.raises(SocialConflict):
            graph.request(HUMAN, CODEX, "又见面了")


# --------------------------------------------------------------------------- #
# scope 默认值 与 群聊降级
# --------------------------------------------------------------------------- #


class TestScopeDefaults:
    """全部用「访客 ↔ 别人的 agent」，避免 owner 全权把结论带偏。"""

    def test_friends_get_conversation_scopes_only(self, graph: SocialGraph):
        befriend(graph, GUEST, CODEX)
        for s in DEFAULT_FRIEND_SCOPES:
            assert graph.can(GUEST, CODEX, s), s
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is False
        assert graph.can(GUEST, CODEX, Scope.ADMIN) is False
        # 唯一的例外：owner 对自己的 agent 天然全权（它是你的，不是邻居）
        assert graph.can(HUMAN, CODEX, Scope.ADMIN) is True

    def test_grants_are_directional(self, graph: SocialGraph):
        befriend(graph, GUEST, CODEX)  # 双方都只拿到默认档
        # `set_grant(A, B, ...)` 调的是「B 能对 A 做什么」，与「A 能对 B 做什么」无关
        graph.set_grant(GUEST, CODEX, [Scope.PEEK, Scope.CHAT, Scope.DELEGATE])
        assert graph.can(CODEX, GUEST, Scope.DELEGATE) is True
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is False

    def test_explicit_delegate_grant_works(self, graph: SocialGraph):
        # CODEX 是同意方，所以是它授权给发起方 GUEST
        befriend(graph, GUEST, CODEX, [Scope.PEEK, Scope.CHAT, Scope.INVITE, Scope.DELEGATE])
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is True
        # 反向没有：GUEST 并没有给 CODEX 执行权
        assert graph.can(CODEX, GUEST, Scope.DELEGATE) is False

    def test_group_channel_only_relaxes_chat(self, graph: SocialGraph):
        befriend(graph, GUEST, CODEX, [Scope.PEEK, Scope.CHAT, Scope.INVITE, Scope.DELEGATE])
        assert graph.can(GUEST, CODEX, Scope.CHAT, via="group:conv-1") is True
        # 关键：群里不给执行权，否则建个群就能驱动别人的 agent
        assert graph.can(GUEST, CODEX, Scope.DELEGATE, via="group:conv-1") is False
        assert graph.can(GUEST, CODEX, Scope.ARTIFACT, via="group:conv-1") is False

    def test_group_scopes_constant_matches_behaviour(self):
        assert set(GROUP_SCOPES) == {Scope.PEEK, Scope.CHAT}

    def test_self_access_always_allowed(self, graph: SocialGraph):
        assert graph.can(HUMAN, HUMAN, Scope.ADMIN) is True

    def test_system_is_never_blocked(self, graph: SocialGraph):
        assert graph.can(SYSTEM, CODEX, Scope.ADMIN) is True
        assert graph.can(CODEX, SYSTEM, Scope.ADMIN) is True


class TestModes:
    def test_strict_refuses_non_friends(self):
        g = SocialGraph(make_members(), mode="strict")
        assert g.can(GUEST, CODEX, Scope.CHAT) is False

    def test_soft_allows_chat_but_never_delegate(self):
        g = SocialGraph(make_members(), mode="soft")
        assert g.can(GUEST, CODEX, Scope.CHAT) is True
        assert g.can(GUEST, CODEX, Scope.DELEGATE) is False
        assert g.can(GUEST, CODEX, Scope.PEEK) is False

    def test_off_is_wide_open(self):
        g = SocialGraph(make_members(), mode="off")
        assert g.enabled is False
        assert g.can(GUEST, CODEX, Scope.ADMIN) is True

    def test_no_members_means_disabled(self):
        g = SocialGraph([], mode="strict")
        assert g.enabled is False
        assert g.can(GUEST, CODEX, Scope.ADMIN) is True
        assert g.visible_contacts(GUEST) is None

    def test_null_guard_is_all_pass(self):
        guard = NullGuard()
        assert guard.can(GUEST, CODEX, Scope.ADMIN) is True
        assert guard.visible_contacts(GUEST) is None
        assert guard.enabled is False


# --------------------------------------------------------------------------- #
# 权限上行闭包
# --------------------------------------------------------------------------- #


class TestUpwardClosure:
    def test_human_owns_everything_by_default(self, graph: SocialGraph):
        assert graph.owned_scopes(HUMAN) == set(Scope)

    def test_agent_inherits_owner_ceiling(self, graph: SocialGraph):
        assert graph.owned_scopes(CODEX) == set(Scope)

    def test_cannot_grant_what_you_do_not_own(self, graph: SocialGraph):
        # 给 QWEN 设一个窄天花板，它就不能把 delegate 转授出去
        graph._members[QWEN].max_scopes = [Scope.PEEK, Scope.CHAT]
        befriend(graph, HUMAN, QWEN, [Scope.PEEK, Scope.CHAT])
        with pytest.raises(SocialNotPermitted) as err:
            graph.set_grant(QWEN, HUMAN, [Scope.DELEGATE])
        assert "上行闭包" in str(err.value)

    def test_ceiling_intersects_owner_ceiling(self, graph: SocialGraph):
        graph._members[HUMAN].max_scopes = [Scope.PEEK, Scope.CHAT, Scope.INVITE]
        assert graph.owned_scopes(CODEX) == {Scope.PEEK, Scope.CHAT, Scope.INVITE}
    def test_ownerless_agent_only_gets_conversation_scopes(self, graph: SocialGraph):
        # 多人类场景：没声明 owner 的 agent 不该拿到执行类权限
        g = SocialGraph(make_members() + [Member(id=ECHO_A, name="孤儿", kind="agent")],
                        mode="strict")
        assert g.owned_scopes(ECHO_A) == set(DEFAULT_FRIEND_SCOPES)

    def test_ownerless_agent_gets_privileges_when_single_human(self, solo: SocialGraph):
        # 单人自用：漏写 owner 不该让 agent 谁都使唤不动
        assert solo.owned_scopes(ECHO_A) == set(Scope)

    def test_accept_cannot_exceed_own_ceiling(self, graph: SocialGraph):
        graph._members[CODEX].max_scopes = [Scope.PEEK, Scope.CHAT]
        graph.request(HUMAN, CODEX, "想加你")
        with pytest.raises(SocialNotPermitted):
            graph.accept(CODEX, HUMAN, [Scope.PEEK, Scope.CHAT, Scope.DELEGATE])

    def test_agent_chain_does_not_blow_the_stack(self):
        # agent 套 agent 的病态配置：深度保护必须生效，不能 RecursionError
        chain = [Member(id="agent:a0", name="a0", kind="agent", owner="agent:a1")]
        for i in range(1, 20):
            chain.append(
                Member(id=f"agent:a{i}", name=f"a{i}", kind="agent", owner=f"agent:a{i + 1}")
            )
        g = SocialGraph(chain, mode="strict")
        assert g.owned_scopes("agent:a0") <= set(Scope)  # 不抛异常即可


# --------------------------------------------------------------------------- #
# owner 代理
# --------------------------------------------------------------------------- #


class TestOwnerProxy:
    def test_owner_has_full_access_to_own_agent(self, graph: SocialGraph):
        assert graph.can(HUMAN, CODEX, Scope.ADMIN) is True

    def test_owner_full_access_can_be_switched_off(self):
        g = SocialGraph(make_members(), mode="strict", owner_full_access=False)
        assert g.can(HUMAN, CODEX, Scope.ADMIN) is False

    def test_stranger_is_not_an_owner(self, graph: SocialGraph):
        assert graph.can(GUEST, CODEX, Scope.CHAT) is False

    def test_owner_can_accept_on_behalf_of_agent(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想用你的算法能力")
        rel = graph.accept(HUMAN, GUEST, as_member=CODEX)
        assert rel.state is RelationState.FRIEND
        assert graph.is_friend(GUEST, CODEX)

    def test_stranger_cannot_accept_on_behalf_of_agent(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        # QWEN 也是 HUMAN 的 agent，但它不能代表 CODEX 做决定
        with pytest.raises(SocialNotPermitted):
            graph.accept(QWEN, HUMAN, as_member=CODEX)

    def test_can_act_for_only_covers_self_and_own_agents(self, graph: SocialGraph):
        assert graph.can_act_for(HUMAN, HUMAN) is True
        assert graph.can_act_for(HUMAN, CODEX) is True
        assert graph.can_act_for(HUMAN, GUEST) is False
        assert graph.can_act_for(GUEST, CODEX) is False

    # --- owner 代 agent 做「关系变更」：grant / revoke / block -------------- #
    # 这几条不是锦上添花：agent 自己不会调 CLI，owner 若只能代它「同意好友」
    # 却不能代它「给执行权」，delegate 就永远授不出去——「聊天权 ≠ 指挥权」
    # 这个开关会卡在关位。

    def test_owner_can_grant_delegate_on_behalf_of_agent(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想用你的算法能力")
        graph.accept(HUMAN, GUEST, as_member=CODEX)
        # 好友默认没有 delegate
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is False
        # owner 代 agent 授权
        graph.set_grant(HUMAN, GUEST, [Scope.CHAT, Scope.DELEGATE], as_member=CODEX)
        assert graph.can(GUEST, CODEX, Scope.DELEGATE) is True

    def test_owner_can_revoke_on_behalf_of_agent(self, graph: SocialGraph):
        graph.request(GUEST, CODEX, "想加你")
        graph.accept(HUMAN, GUEST, as_member=CODEX)
        assert graph.is_friend(GUEST, CODEX) is True
        graph.revoke(HUMAN, GUEST, as_member=CODEX)
        assert graph.is_friend(GUEST, CODEX) is False

    def test_owner_can_block_on_behalf_of_agent(self, graph: SocialGraph):
        graph.block(HUMAN, GUEST, as_member=CODEX)
        assert graph.is_blocked(GUEST, CODEX) is True
        graph.unblock(HUMAN, GUEST, as_member=CODEX)
        assert graph.is_blocked(GUEST, CODEX) is False

    def test_stranger_cannot_grant_on_behalf_of_agent(self, graph: SocialGraph):
        # GUEST 不拥有 CODEX，无权代它授权
        with pytest.raises(SocialNotPermitted):
            graph.set_grant(GUEST, HUMAN, [Scope.CHAT], as_member=CODEX)

    def test_effective_owner_prefers_explicit_declaration(self, graph: SocialGraph):
        assert graph.effective_owner(CODEX) == HUMAN

    def test_effective_owner_is_none_for_humans(self, graph: SocialGraph):
        assert graph.effective_owner(HUMAN) is None

    def test_warn_undeclared_owners_only_in_multi_human(self, graph: SocialGraph, solo):
        g = SocialGraph(make_members() + [Member(id=ECHO_A, name="孤儿", kind="agent")],
                        mode="strict")
        assert g.warn_undeclared_owners() == [ECHO_A]
        assert solo.warn_undeclared_owners() == []


# --------------------------------------------------------------------------- #
# 可见性 与 隐私
# --------------------------------------------------------------------------- #


class TestVisibility:
    def test_strict_hides_non_friends(self, graph: SocialGraph):
        visible = graph.visible_contacts(HUMAN)
        assert visible == {"codex", "qwen-office"}  # 自己的两个 agent

    def test_friend_appears_after_acceptance(self, graph: SocialGraph):
        graph.ensure_agents([("echo-a", "回显 A")])
        assert "echo-a" not in (graph.visible_contacts(HUMAN) or set())
        befriend(graph, HUMAN, ECHO_A)
        assert "echo-a" in (graph.visible_contacts(HUMAN) or set())

    def test_soft_mode_shows_all_agents(self):
        g = SocialGraph(make_members(), mode="soft")
        assert g.visible_contacts(HUMAN) == {"codex", "qwen-office"}

    def test_disabled_means_no_filtering(self, solo: SocialGraph):
        g = SocialGraph([m for m in solo.all_members()], mode="off")
        assert g.visible_contacts(HUMAN) is None

    def test_agent_never_sees_itself(self, graph: SocialGraph):
        assert "codex" not in (graph.visible_contacts(CODEX) or set())


class TestPrivacy:
    def test_search_excludes_private_members(self, graph: SocialGraph):
        ids = {m["id"] for m in graph.search("")}
        assert CODEX in ids
        assert QWEN not in ids  # discoverable=private

    def test_search_filters_by_keyword(self, graph: SocialGraph):
        ids = {m["id"] for m in graph.search("codex")}
        assert ids == {CODEX}

    def test_search_matches_bio(self, graph: SocialGraph):
        assert {m["id"] for m in graph.search("算法")} == {CODEX}

    def test_private_members_are_still_addressable(self, graph: SocialGraph):
        # 私有只挡「被发现」，不挡「被指名申请」——否则新 agent 永远加不上
        rel = graph.request(HUMAN, QWEN, "我知道你的 id")
        assert rel.state is RelationState.PENDING

    def test_relations_of_returns_only_my_edges(self, graph: SocialGraph):
        befriend(graph, GUEST, CODEX)
        assert graph.relations_of(HUMAN) == []
        assert len(graph.relations_of(GUEST)) == 1

    def test_describe_separates_both_directions(self, graph: SocialGraph):
        # CODEX 发起申请；HUMAN 同意时额外给了 CODEX `delegate`
        befriend(graph, CODEX, HUMAN, [Scope.PEEK, Scope.CHAT, Scope.DELEGATE])
        d = graph.describe(graph.relation(HUMAN, CODEX), HUMAN)
        assert d["peer"] == CODEX
        assert d["myScopes"] == ["peek", "chat", "invite"]       # 我能对他做什么
        assert d["theirScopes"] == ["peek", "chat", "delegate"]  # 他能对我做什么

    def test_describe_reports_block_direction(self, graph: SocialGraph):
        graph.block(CODEX, HUMAN)
        mine = graph.describe(graph.relation(HUMAN, CODEX), CODEX)
        theirs = graph.describe(graph.relation(HUMAN, CODEX), HUMAN)
        assert mine["iBlocked"] is True
        assert theirs["blockedByPeer"] is True

    def test_card_flags_autonomous_agents(self):
        g = SocialGraph(
            [Member(id=CODEX, name="Codex", kind="agent",
                    autonomy={"request": {"enabled": True}})],
            mode="strict",
        )
        assert g.card(g.get_member(CODEX))["autonomous"] is True


# --------------------------------------------------------------------------- #
# 收件箱 / 发件箱 / 名片
# --------------------------------------------------------------------------- #


class TestInboxOutbox:
    def test_inbox_lists_requests_to_me(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        assert [r["peer"] for r in graph.inbox(CODEX)] == [HUMAN]
        assert graph.inbox(HUMAN) == []

    def test_outbox_lists_my_pending_requests(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        assert [r["peer"] for r in graph.outbox(HUMAN)] == [CODEX]
        assert graph.outbox(CODEX) == []

    def test_owner_inbox_does_not_leak_agent_requests(self, graph: SocialGraph):
        # 收件箱是「成员粒度」的：想看 agent 的申请要用 --as，
        # 否则多租户下 owner 会看到一堆不属于自己的待办
        graph.request(GUEST, CODEX, "想加你")
        assert graph.inbox(HUMAN) == []
        assert len(graph.inbox(CODEX)) == 1

    def test_me_aggregates_counts_and_ceiling(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        me = graph.me(HUMAN)
        assert me["member"]["id"] == HUMAN
        assert me["enabled"] is True
        assert me["mode"] == "strict"
        assert me["counts"]["pendingOut"] == 1
        assert me["counts"]["friends"] == 0
        assert set(me["member"]["ownedScopes"]) == ALL_SCOPES

    def test_me_reports_effective_owner_for_agent(self, graph: SocialGraph):
        assert graph.me(CODEX)["member"]["owner"] == HUMAN

    def test_me_works_when_disabled(self, solo: SocialGraph):
        g = SocialGraph([], mode="strict")
        assert g.me(USER)["enabled"] is False


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #


class TestAudit:
    def test_every_relation_change_is_recorded(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.accept(CODEX, HUMAN)
        graph.set_grant(HUMAN, CODEX, [Scope.CHAT])
        graph.revoke(HUMAN, CODEX)
        actions = [a["action"] for a in reversed(graph.trail())]
        assert actions == ["request", "accept", "grant", "revoke"]

    def test_trail_is_newest_first(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.cancel(HUMAN, CODEX)
        assert graph.trail()[0]["action"] == "cancel"

    def test_trail_can_filter_by_peer(self, graph: SocialGraph):
        graph.request(HUMAN, CODEX, "想加你")
        graph.request(HUMAN, QWEN, "想加你")
        assert {a["peer"] for a in graph.trail(CODEX)} == {CODEX}

    def test_no_silent_relationship_changes(self, graph: SocialGraph):
        graph.block(GUEST, CODEX)
        graph.unblock(GUEST, CODEX)
        assert {a["action"] for a in graph.trail()} == {"block", "unblock"}


# --------------------------------------------------------------------------- #
# 身份（token）
# --------------------------------------------------------------------------- #


class TestTokens:
    def test_resolve_token(self):
        g = SocialGraph(
            [Member(id=HUMAN, name="海鱼", kind="human", tokens=["tok-h"]),
             Member(id=CODEX, name="Codex", kind="agent", tokens=["tok-c"])],
            mode="strict",
        )
        assert g.resolve_token("tok-h").id == HUMAN
        assert g.resolve_token("tok-c").id == CODEX

    def test_unknown_token_resolves_to_none(self, graph: SocialGraph):
        assert graph.resolve_token("nope") is None

    def test_empty_token_resolves_to_none(self, graph: SocialGraph):
        assert graph.resolve_token("") is None

    def test_any_tokens(self, graph: SocialGraph):
        assert graph.any_tokens() is False
        graph._members[HUMAN].tokens = ["tok-h"]
        assert graph.any_tokens() is True

    def test_any_tokens_ignores_empty_expansions(self, tmp_path):
        # `tokens: ["${A2A_TOKEN_X}"]` 在变量没设时展开成 [""]。
        # 若按「列表非空」判断，会一边认不出 token（403）一边拒绝匿名（401），
        # 等于把部署者锁在门外。
        p = tmp_path / "members.yaml"
        p.write_text(
            yaml.safe_dump(
                {"members": [{"id": HUMAN, "name": "海鱼", "tokens": ["${NOPE_VAR}"]}]}
            ),
            encoding="utf-8",
        )
        g = SocialGraph(SocialGraph.load_members(p), mode="strict")
        assert g.any_tokens() is False
        assert g.resolve_token("") is None
        assert [m.tokens for m in g.all_members()] == [[]]

    def test_shipped_example_loads(self, monkeypatch):
        from pathlib import Path

        monkeypatch.delenv("A2A_TOKEN_SEAFISH", raising=False)
        monkeypatch.delenv("A2A_TOKEN_PARTNER", raising=False)
        example = (
            Path(__file__).resolve().parent.parent / "config" / "members.example.yaml"
        )
        members = SocialGraph.load_members(example)
        ids = {m.id for m in members}
        assert {HUMAN, CODEX, "agent:partner-ocr", "human:partner"} <= ids
        g = SocialGraph(members, mode="strict")
        assert g.enabled is True
        # 例子里的 agent 都该有 owner，否则拿不到执行类权限
        assert g.get_member(CODEX).owner == HUMAN
        # token 全走环境变量占位符 → 未设置时为「尚未配身份」状态
        assert g.any_tokens() is False


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


class TestPersistence:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "relations.json"
        g1 = SocialGraph(make_members(), mode="strict", path=path)
        g1.request(HUMAN, CODEX, "想加你")
        g1.accept(CODEX, HUMAN, [Scope.PEEK, Scope.CHAT, Scope.DELEGATE])

        g2 = SocialGraph(make_members(), mode="strict", path=path)
        assert g2.is_friend(HUMAN, CODEX)
        assert g2.can(HUMAN, CODEX, Scope.DELEGATE) is True
        assert g2.relation(HUMAN, CODEX).requestMessage == "想加你"

    def test_persisted_payload_is_versioned_json(self, tmp_path):
        path = tmp_path / "relations.json"
        g = SocialGraph(make_members(), mode="strict", path=path)
        g.request(HUMAN, CODEX, "想加你")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert len(raw["relations"]) == 1

    def test_audit_survives_restart(self, tmp_path):
        path = tmp_path / "relations.json"
        g1 = SocialGraph(make_members(), mode="strict", path=path)
        g1.request(HUMAN, CODEX, "想加你")
        g2 = SocialGraph(make_members(), mode="strict", path=path)
        assert g2.trail()[0]["action"] == "request"

    def test_corrupt_file_does_not_block_startup(self, tmp_path):
        path = tmp_path / "relations.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        g = SocialGraph(make_members(), mode="strict", path=path)
        assert g.enabled is True
        assert g.friends_of(HUMAN) == []

    def test_damaged_record_is_skipped(self, tmp_path):
        path = tmp_path / "relations.json"
        path.write_text(
            json.dumps({
                "version": 1,
                "relations": [{"pair": ["a"], "state": "not-a-state"}],
                "audit": [],
            }),
            encoding="utf-8",
        )
        g = SocialGraph(make_members(), mode="strict", path=path)
        assert g._rels == {}

    def test_missing_file_is_fine(self, tmp_path):
        g = SocialGraph(make_members(), mode="strict", path=tmp_path / "nope.json")
        assert g.enabled is True

    def test_load_members_from_yaml(self, tmp_path):
        p = tmp_path / "members.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "members": [
                        {"id": HUMAN, "name": "海鱼", "kind": "human", "tokens": ["tok-h"]},
                        {"id": CODEX, "name": "Codex", "kind": "agent", "owner": HUMAN,
                         "discoverable": "public"},
                    ]
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        members = SocialGraph.load_members(p)
        assert [m.id for m in members] == [HUMAN, CODEX]
        g = SocialGraph(members, mode="strict")
        assert g.enabled is True

    def test_load_members_missing_file_returns_empty(self, tmp_path):
        assert SocialGraph.load_members(tmp_path / "nope.yaml") == []
        assert SocialGraph.load_members(None) == []

    def test_load_members_skips_invalid_entries(self, tmp_path):
        p = tmp_path / "members.yaml"
        p.write_text(
            yaml.safe_dump(
                {"members": [{"name": "缺 id"}, {"id": HUMAN, "name": "海鱼"}]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        assert [m.id for m in SocialGraph.load_members(p)] == [HUMAN]

    def test_ensure_agents_fills_in_registry_nodes(self, graph: SocialGraph):
        graph.ensure_agents([("echo-a", "回显 A"), ("qwen-office", "会被忽略")])
        assert graph.get_member(ECHO_A).name == "回显 A"
        # 已声明的成员不被覆盖
        assert graph.get_member(QWEN).name == "千问办公"
        # 合成出来的节点默认私有：挡「被发现」，不挡「被指名申请」
        assert graph.get_member(ECHO_A).discoverable == "private"


# =========================================================================== #
# 门禁接进会话层
# =========================================================================== #


@pytest.fixture
def guarded(registry, tmp_path):
    """带门禁的会话层：两个人类 + 两个归 human:seafish 的 agent。"""
    from a2a_hub.social import SocialHub

    members = [
        Member(id=HUMAN, name="海鱼", kind="human", tokens=["tok-h"]),
        Member(id=GUEST, name="访客", kind="human", tokens=["tok-g"]),
        Member(id=ECHO_A, name="回显 A", kind="agent", owner=HUMAN, discoverable="public"),
        Member(id=ECHO_B, name="回显 B", kind="agent", owner=HUMAN, discoverable="public"),
    ]
    g = SocialGraph(members, mode="strict", path=tmp_path / "relations.json")
    g.ensure_agents((r.id, r.name) for r in registry.list_records())
    return SocialHub(registry, registry.bus, guard=g), g


class TestChatGate:
    async def test_owner_can_talk_to_own_agent(self, guarded):
        hub, _ = guarded
        conv = hub.open_direct("echo-a", owner=HUMAN)
        res = await hub.send(conv.id, "你好", sender=HUMAN)
        assert res["woke"] == ["echo-a"]
        assert res["refused"] == []

    async def test_stranger_is_refused_with_a_visible_note(self, guarded):
        hub, _ = guarded
        conv = hub.open_direct("echo-a", owner=GUEST)
        res = await hub.send(conv.id, "在吗", sender=GUEST)
        assert res["woke"] == []
        assert res["refused"] == ["echo-a"]
        # 被挡下必须看得见：静默丢消息是协作场景最糟的体验
        notes = [m for m in hub.get(conv.id).messages if m.kind == "system"]
        assert notes and "好友" in notes[-1].text

    async def test_friend_can_chat_after_acceptance(self, guarded):
        hub, g = guarded
        g.request(GUEST, ECHO_A, "想用你的评审能力")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)  # owner 代表 agent 同意
        conv = hub.open_direct("echo-a", owner=GUEST)
        res = await hub.send(conv.id, "你好", sender=GUEST)
        assert res["woke"] == ["echo-a"]
        assert res["refused"] == []

    async def test_friendship_does_not_grant_delegation(self, guarded):
        hub, g = guarded
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        assert g.can(GUEST, ECHO_A, Scope.CHAT) is True
        assert g.can(GUEST, ECHO_A, Scope.DELEGATE) is False

    async def test_contacts_are_filtered_by_membership(self, guarded):
        hub, g = guarded
        assert {c["id"] for c in hub.contacts(GUEST)} == set()
        assert {"echo-a", "echo-b"} <= {c["id"] for c in hub.contacts(HUMAN)}

    async def test_contacts_report_my_scopes(self, guarded):
        hub, _ = guarded
        cal = {c["id"]: c["scopes"] for c in hub.contacts(HUMAN)}
        assert "admin" in cal["echo-a"]  # owner 对自己的 agent 全权

    async def test_contacts_unfiltered_without_a_guard(self, registry):
        from a2a_hub.social import SocialHub

        hub = SocialHub(registry, registry.bus)
        assert {"echo-a", "echo-b"} <= {c["id"] for c in hub.contacts()}

    async def test_creating_a_group_requires_invite(self, guarded):
        hub, _ = guarded
        # echo-a 没给 GUEST `invite`，所以拉不动它进群
        with pytest.raises(PermissionError):
            hub.create_group(["echo-a"], actor=GUEST)

    async def test_owner_can_create_a_group_with_own_agent(self, guarded):
        hub, _ = guarded
        conv = hub.create_group(["echo-a"], actor=HUMAN)
        assert conv.kind == "group"
        assert conv.members == ["echo-a"]

    async def test_direct_conversations_are_scoped_per_owner(self, guarded):
        hub, g = guarded
        a = hub.open_direct("echo-a", owner=HUMAN)
        b = hub.open_direct("echo-a", owner=GUEST)
        # 旧实现只看 members==[agent]，两个人会串到同一个会话里
        assert a.id != b.id

    async def test_conversation_visibility(self, guarded):
        hub, _ = guarded
        conv = hub.open_direct("echo-a", owner=HUMAN)
        assert hub.can_view(conv, HUMAN) is True
        assert hub.can_view(conv, GUEST) is False

    async def test_list_conversations_only_mine(self, guarded):
        hub, _ = guarded
        hub.open_direct("echo-a", owner=HUMAN)
        assert len(hub.list_conversations(HUMAN)) == 1
        assert hub.list_conversations(GUEST) == []


# =========================================================================== #
# HTTP 层：社交端点 + 阶段 1 补掉的四个安全缺口
# =========================================================================== #


TEST_MEMBERS = {
    "members": [
        {"id": HUMAN, "name": "海鱼", "kind": "human", "discoverable": "circle",
         "tokens": ["tok-h"]},
        {"id": GUEST, "name": "访客", "kind": "human", "discoverable": "public",
         "tokens": ["tok-g"], "max_scopes": ["peek", "chat", "invite"]},
        {"id": ECHO_A, "name": "回显 A", "kind": "agent", "owner": HUMAN,
         "discoverable": "public", "bio": "代码评审与写作"},
        {"id": ECHO_B, "name": "回显 B", "kind": "agent", "owner": HUMAN,
         "discoverable": "private"},
    ]
}

HUMAN_H = {"Authorization": "Bearer tok-h"}
GUEST_H = {"Authorization": "Bearer tok-g"}


@pytest.fixture
def members_env(tmp_path, agents_file, monkeypatch):
    from a2a_hub import config as cfg
    from a2a_hub import registry as reg

    mf = tmp_path / "members.yaml"
    mf.write_text(yaml.safe_dump(TEST_MEMBERS, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("A2A_AGENTS_FILE", str(agents_file))
    monkeypatch.setenv("A2A_STORE", "memory")
    monkeypatch.setenv("A2A_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("A2A_API_TOKEN", "")
    monkeypatch.delenv("A2A_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("A2A_MEMBERS_FILE", str(mf))
    monkeypatch.setenv("A2A_RELATIONS_FILE", str(tmp_path / "relations.json"))
    monkeypatch.setenv("A2A_SOCIAL_MODE", "strict")
    cfg._settings = None
    reg._registry = None
    yield cfg.get_settings()
    cfg._settings = None
    reg._registry = None


@pytest.fixture
def mclient(members_env):
    from fastapi.testclient import TestClient

    import a2a_hub.server as srv

    srv.hub = srv.Hub()
    with TestClient(srv.app) as c:
        yield c


class TestSocialApi:
    def test_me_identifies_the_caller(self, mclient):
        r = mclient.get("/social/me", headers=HUMAN_H)
        assert r.status_code == 200
        body = r.json()
        assert body["member"]["id"] == HUMAN
        assert body["enabled"] is True
        assert body["mode"] == "strict"

    def test_unknown_token_is_403(self, mclient):
        r = mclient.get("/social/me", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 403

    def test_missing_token_is_401(self, mclient):
        assert mclient.get("/social/me").status_code == 401

    def test_search_hides_private_members(self, mclient):
        r = mclient.get("/social/members", headers=GUEST_H)
        ids = {m["id"] for m in r.json()["members"]}
        assert ECHO_A in ids
        assert ECHO_B not in ids

    def test_relations_are_private_to_the_caller(self, mclient):
        mclient.post("/social/requests", json={"to": ECHO_A, "message": "想加你"},
                     headers=HUMAN_H)
        assert mclient.get("/social/relations", headers=GUEST_H).json()["count"] == 0
        assert mclient.get("/social/relations", headers=HUMAN_H).json()["count"] == 1

    def test_request_requires_a_reason(self, mclient):
        r = mclient.post("/social/requests", json={"to": ECHO_A}, headers=HUMAN_H)
        assert r.status_code == 400

    def test_request_missing_target_is_422(self, mclient):
        r = mclient.post("/social/requests", json={"message": "你好"}, headers=HUMAN_H)
        assert r.status_code == 422

    def test_full_friend_flow_over_http(self, mclient):
        # 1) 访客申请加 echo-a
        r = mclient.post("/social/requests",
                         json={"to": ECHO_A, "message": "想用你的写作能力"},
                         headers=GUEST_H)
        assert r.status_code == 200, r.text
        # 2) owner 以 echo-a 的视角看到这条申请
        r = mclient.get("/social/requests", params={"box": "in", "as": ECHO_A},
                        headers=HUMAN_H)
        assert r.status_code == 200, r.text
        assert r.json()["member"] == ECHO_A
        assert [x["peer"] for x in r.json()["requests"]] == [GUEST]
        # 3) owner 代表 echo-a 同意，并且只给对话档
        r = mclient.post("/social/requests/accept",
                         json={"peer": GUEST, "scopes": ["peek", "chat"], "as": ECHO_A},
                         headers=HUMAN_H)
        assert r.status_code == 200, r.text
        # 4) 访客现在能跟 echo-a 说话了，但没有 delegate
        r = mclient.post("/im/conversations", json={"kind": "direct", "agent": "echo-a"},
                         headers=GUEST_H)
        conv = r.json()["id"]
        r = mclient.post(f"/im/conversations/{conv}/messages", json={"text": "你好"},
                         headers=GUEST_H)
        assert r.json()["woke"] == ["echo-a"]
        r = mclient.get("/social/relations", headers=GUEST_H)
        assert r.json()["relations"][0]["myScopes"] == ["peek", "chat"]
        assert r.json()["relations"][0]["theirScopes"] == ["peek", "chat", "invite"]

    def test_grant_violating_upward_closure_is_403(self, mclient):
        # 让 GUEST 与 ECHO_A 成为好友，然后 GUEST 试图授予 delegate
        mclient.post("/social/requests", json={"to": ECHO_A, "message": "想加你"},
                     headers=GUEST_H)
        mclient.post("/social/requests/accept",
                     json={"peer": GUEST, "scopes": ["peek", "chat"], "as": ECHO_A},
                     headers=HUMAN_H)
        r = mclient.patch(f"/social/relations/{ECHO_A}", json={"scopes": ["delegate"]},
                          headers=GUEST_H)
        assert r.status_code == 403
        assert "上行闭包" in r.json()["detail"]

    def test_grant_requires_friendship(self, mclient):
        r = mclient.patch(f"/social/relations/{ECHO_A}", json={"scopes": ["chat"]},
                          headers=GUEST_H)
        assert r.status_code == 409

    def test_block_then_unblock(self, mclient):
        assert mclient.post(f"/social/relations/{HUMAN}/block",
                            headers=GUEST_H).status_code == 200
        r = mclient.post("/social/requests", json={"to": GUEST, "message": "在吗"},
                         headers=HUMAN_H)
        # 被拉黑时中性失败，不透露原因
        assert r.status_code in (400, 409)
        assert "拉黑" not in r.text
        assert mclient.delete(f"/social/relations/{HUMAN}/block",
                              headers=GUEST_H).status_code == 200

    def test_unblock_without_block_is_409(self, mclient):
        assert mclient.delete(f"/social/relations/{ECHO_A}/block",
                              headers=GUEST_H).status_code == 409

    def test_revoke_then_chat_is_refused(self, mclient):
        mclient.post("/social/requests", json={"to": ECHO_A, "message": "想加你"},
                     headers=GUEST_H)
        mclient.post("/social/requests/accept",
                     json={"peer": GUEST, "scopes": ["peek", "chat"], "as": ECHO_A},
                     headers=HUMAN_H)
        r = mclient.post("/im/conversations", json={"kind": "direct", "agent": "echo-a"},
                         headers=GUEST_H)
        conv = r.json()["id"]
        assert mclient.delete(f"/social/relations/{ECHO_A}",
                              headers=GUEST_H).status_code == 200
        r = mclient.post(f"/im/conversations/{conv}/messages", json={"text": "还在吗"},
                         headers=GUEST_H)
        # 删好友后已有单聊会归档（可读不可写）。所以「解除了关系就交流不了」
        # 有两种可接受的落点：直接拒绝发送，或发出但无人被唤醒。
        if r.status_code == 200:
            assert r.json()["woke"] == []
        else:
            assert r.status_code == 422
            assert "归档" in r.text
        # 历史仍然可读——归档不是删除，审计链不能断
        assert mclient.get(f"/im/conversations/{conv}", headers=GUEST_H).status_code == 200

    def test_audit_is_scoped_to_me(self, mclient):
        mclient.post("/social/requests", json={"to": ECHO_A, "message": "想加你"},
                     headers=HUMAN_H)
        assert mclient.get("/social/audit", headers=GUEST_H).json()["count"] == 0
        assert mclient.get("/social/audit", headers=HUMAN_H).json()["count"] == 1

    def test_agent_card_declares_x_social(self, mclient):
        card = mclient.get("/.well-known/agent.json").json()
        assert any(e["uri"].endswith("x-social") for e in card.get("extensions", []))
        assert card["metadata"]["social"] == {"enabled": True, "mode": "strict"}
        assert "社交门禁" in card["description"]

    def test_agent_card_has_no_social_extension_when_disabled(self, client):
        card = client.get("/.well-known/agent.json").json()
        assert card.get("extensions") is None
        assert "social" not in card.get("metadata", {})


class TestOwnerReadProxy:
    """owner 必须能看自己 agent 的视角，否则 agent 收到的申请没人处理。"""

    def test_owner_can_view_agent_perspective(self, mclient):
        r = mclient.get("/social/me", params={"as": ECHO_A}, headers=HUMAN_H)
        assert r.status_code == 200
        assert r.json()["member"]["id"] == ECHO_A
        assert r.json()["member"]["owner"] == HUMAN

    def test_stranger_cannot_view_agent_perspective(self, mclient):
        r = mclient.get("/social/me", params={"as": ECHO_A}, headers=GUEST_H)
        assert r.status_code == 403

    def test_owner_can_use_as_on_relations_and_contacts(self, mclient):
        assert mclient.get("/social/relations", params={"as": ECHO_A},
                           headers=HUMAN_H).status_code == 200
        assert mclient.get("/im/contacts", params={"as": ECHO_A},
                           headers=HUMAN_H).status_code == 200

    def test_as_without_graph_is_not_required(self, client):
        # 不带 as 时不该因为「门禁没开」而 409——无配置部署依然能用
        assert client.get("/social/me").status_code == 200
        assert client.get("/im/contacts").status_code == 200


class TestSecurityHoles:
    """阶段 1 顺手补掉的四个缺口，各留一条回归。"""

    def _direct(self, mclient, headers=HUMAN_H, agent="echo-a"):
        r = mclient.post("/im/conversations", json={"kind": "direct", "agent": agent},
                         headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def test_hole1_im_events_requires_auth(self, mclient):
        """缺口 1：SSE 端点曾完全不鉴权，谁都能订阅别人的会话。"""
        conv = self._direct(mclient)
        assert mclient.get(f"/im/conversations/{conv}/events").status_code == 401

    def test_hole1_im_events_hides_other_peoples_conversations(self, mclient):
        conv = self._direct(mclient, HUMAN_H)
        r = mclient.get(f"/im/conversations/{conv}/events", headers=GUEST_H)
        assert r.status_code == 403

    def test_hole1_history_is_not_readable_by_others(self, mclient):
        conv = self._direct(mclient, HUMAN_H)
        assert mclient.get(f"/im/conversations/{conv}", headers=GUEST_H).status_code == 403

    def test_hole2_mark_read_ignores_body_reader(self, mclient):
        """缺口 2：``reader`` 曾取自请求体，谁都能替别人清未读。"""
        import a2a_hub.server as srv

        conv = self._direct(mclient)
        mclient.post(f"/im/conversations/{conv}/messages", json={"text": "你好"},
                     headers=HUMAN_H)
        r = mclient.post(f"/im/conversations/{conv}/read",
                         json={"reader": GUEST}, headers=HUMAN_H)
        assert r.status_code == 200
        reads = srv.hub.social.get(conv).reads
        assert GUEST not in reads          # 伪造的 reader 被忽略
        assert HUMAN in reads              # 真实的调用者被记录

    def test_hole2_sender_is_taken_from_auth_not_body(self, mclient):
        import a2a_hub.server as srv

        conv = self._direct(mclient)
        mclient.post(f"/im/conversations/{conv}/messages",
                     json={"text": "冒充一下", "sender": ECHO_B}, headers=HUMAN_H)
        # 只看这条消息——后台的 echo-a 回复也会落在同一个会话里
        mine = [m for m in srv.hub.social.get(conv).messages if m.text == "冒充一下"]
        assert len(mine) == 1
        assert mine[0].sender == HUMAN

    def test_hole3_console_requires_token(self, mclient):
        """缺口 3：``/console`` 曾裸奔，任何人都能打开控制台。"""
        assert mclient.get("/console", follow_redirects=False).status_code == 401

    def test_hole3_console_rejects_wrong_token(self, mclient):
        r = mclient.get("/console", params={"token": "nope"}, follow_redirects=False)
        assert r.status_code == 401

    def test_hole3_console_accepts_member_token_and_sets_cookie(self, mclient):
        r = mclient.get("/console", params={"token": "tok-h"}, follow_redirects=False)
        assert r.status_code == 200
        assert r.cookies.get("a2a_token") == "tok-h"

    def test_hole3_console_works_without_any_token_config(self, client):
        # 没配 token 的本地部署不该被锁在门外
        assert client.get("/console", follow_redirects=False).status_code == 200

    def test_hole4_every_rpc_carries_an_identity(self, mclient):
        """缺口 4：单一 token 模式下没有「谁在调用」，更没有审计。"""
        r = mclient.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "social/me", "params": {}},
            headers=GUEST_H,
        )
        assert r.json()["result"]["member"]["id"] == GUEST

    def test_hole4_relation_changes_are_audited(self, mclient):
        import a2a_hub.server as srv

        mclient.post("/social/requests", json={"to": ECHO_A, "message": "想加你"},
                     headers=HUMAN_H)
        trail = srv.hub.social_graph.trail()
        assert trail and trail[0]["action"] == "request"
        assert trail[0]["actor"] == HUMAN


class TestRpcSocial:
    async def test_methods_are_registered(self):
        from a2a_hub.rpc import JsonRpcDispatcher

        methods = JsonRpcDispatcher.supported_methods()
        for m in ("social/me", "social/members", "social/relations", "social/requests",
                  "social/request", "social/accept", "social/reject", "social/grant",
                  "social/revoke", "social/block"):
            assert m in methods

    async def test_social_methods_denied_without_a_graph(self, dispatcher):
        res = await dispatcher.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "social/me", "params": {}}
        )
        assert res["error"]["code"] == -32008
        assert "hint" in res["error"]["data"]

    async def test_request_then_accept_via_rpc(self, registry, tmp_path):
        from a2a_hub.orchestrator import Orchestrator
        from a2a_hub.rpc import JsonRpcDispatcher
        from a2a_hub.social import SocialHub

        g = SocialGraph([Member(id=HUMAN, kind="human"), Member(id=GUEST, kind="human"),
                         Member(id=ECHO_A, kind="agent", owner=HUMAN)],
                        mode="strict", path=tmp_path / "r.json")
        hub = SocialHub(registry, registry.bus, guard=g)
        d = JsonRpcDispatcher(registry, Orchestrator(registry, registry.bus), hub, graph=g)

        res = await d.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "social/request",
             "params": {"to": ECHO_A, "message": "想用你的能力"}},
            actor=GUEST,
        )
        assert res["result"]["state"] == "pending"
        assert res["result"]["requestedBy"] == GUEST

        res = await d.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "social/accept",
             "params": {"peer": GUEST, "scopes": ["peek", "chat"], "as": ECHO_A}},
            actor=HUMAN,
        )
        assert res["result"]["state"] == "friend"
        assert g.can(GUEST, ECHO_A, Scope.CHAT) is True
        assert g.can(GUEST, ECHO_A, Scope.DELEGATE) is False

    async def test_rpc_actor_comes_from_handle_not_params(self, registry, tmp_path):
        from a2a_hub.orchestrator import Orchestrator
        from a2a_hub.rpc import JsonRpcDispatcher
        from a2a_hub.social import SocialHub

        g = SocialGraph([Member(id=HUMAN, kind="human"), Member(id=GUEST, kind="human")],
                        mode="strict", path=tmp_path / "r.json")
        hub = SocialHub(registry, registry.bus, guard=g)
        d = JsonRpcDispatcher(registry, Orchestrator(registry, registry.bus), hub, graph=g)

        res = await d.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "social/me",
             "params": {"as": GUEST, "actor": HUMAN}},
            actor=HUMAN,
        )
        # `actor` 参数不参与身份判定；`as` 需要归属校验（人类不能代表人类）
        assert res["error"]["code"] == -32008

    async def test_im_methods_pass_the_actor_to_the_gate(self, registry, tmp_path):
        from a2a_hub.orchestrator import Orchestrator
        from a2a_hub.rpc import JsonRpcDispatcher
        from a2a_hub.social import SocialHub

        g = SocialGraph(
            [Member(id=HUMAN, kind="human"), Member(id=GUEST, kind="human"),
             Member(id=ECHO_A, kind="agent", owner=HUMAN)],
            mode="strict", path=tmp_path / "r.json",
        )
        g.ensure_agents((r.id, r.name) for r in registry.list_records())
        hub = SocialHub(registry, registry.bus, guard=g)
        d = JsonRpcDispatcher(registry, Orchestrator(registry, registry.bus), hub, graph=g)

        res = await d.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "im/open",
             "params": {"agentId": "echo-a"}},
            actor=GUEST,
        )
        cid = res["result"]["id"]
        res = await d.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "im/send",
             "params": {"conversationId": cid, "text": "你好"}},
            actor=GUEST,
        )
        assert res["result"]["woke"] == []
        assert res["result"]["refused"] == ["echo-a"]

