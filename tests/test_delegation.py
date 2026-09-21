"""阶段 2：`delegate` 二级门禁 + 群聊边界 + A2A 兼容。

这一阶段的核心命题只有一句：

> **能聊天 ≠ 能指挥你干活。**

`chat` 门禁在阶段 1 就落到了会话层（IM 发消息）。但真正「花对方额度、
动对方文件」的入口是 A2A 任务层——``message/send``、``message/stream``、
``collab/run``。加了好友只默认给对话档，这些入口必须**额外**拿到 ``delegate``。

测试死盯四条：

1. 非好友 / 好友但没给 ``delegate`` → 执行类调用一律拒绝，且拒绝载荷**能指路**。
2. ``delegate`` **单向不对称**：A 给 B 不等于 B 给 A。
3. 群聊通道**只放宽 chat**，绝不继承执行权；建群本身还要 ``invite``。
4. 没 ``members.yaml`` 时全部放行 —— 与 v0.3.0 逐字节一致。

再加一条阶段 2 补上的：**删好友 / 拉黑后已有单聊归档**（可读不可写）。
"""

from __future__ import annotations

import pytest

from a2a_hub.models import JsonRpcErrorCodes
from a2a_hub.orchestrator import Orchestrator
from a2a_hub.relations import (
    GROUP_SCOPES,
    Member,
    RefusalReason,
    Scope,
    SocialGraph,
    SocialNotPermitted,
)
from a2a_hub.rpc import JsonRpcDispatcher
from a2a_hub.social import SocialHub

HUMAN = "human:seafish"
GUEST = "human:guest"
ECHO_A = "agent:echo-a"
ECHO_B = "agent:echo-b"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def ctx(registry, tmp_path):
    """带门禁的完整装配：关系图 + 会话层 + 编排器 + RPC 分发器。

    刻意把四个组件用**同一个** graph 串起来——门禁只有一处实现，
    各层共用，这个夹具就是那条纪律的可执行版本。
    """
    members = [
        Member(id=HUMAN, name="海鱼", kind="human"),
        Member(id=GUEST, name="访客", kind="human", discoverable="public"),
        Member(id=ECHO_A, name="回显 A", kind="agent", owner=HUMAN, discoverable="public"),
        Member(id=ECHO_B, name="回显 B", kind="agent", owner=HUMAN, discoverable="public"),
    ]
    g = SocialGraph(members, mode="strict", path=tmp_path / "relations.json")
    g.ensure_agents((r.id, r.name) for r in registry.list_records())
    hub = SocialHub(registry, registry.bus, guard=g)
    orch = Orchestrator(registry, registry.bus, guard=g)
    disp = JsonRpcDispatcher(registry, orch, hub, graph=g)
    return {"graph": g, "hub": hub, "orch": orch, "disp": disp, "registry": registry}


async def _send(disp, agent, text="你好", actor=GUEST):
    return await disp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"agentId": agent, "message": text},
        },
        actor=actor,
    )


# --------------------------------------------------------------------------- #
# 判定入口的自洽性
# --------------------------------------------------------------------------- #


class TestWhyNotMirrorsCan:
    """``can()`` 必须是 ``why_not()`` 的布尔化。

    两份分开写的条件迟早会漂移，而那正是权限系统最不该出现的 bug。
    """

    def test_can_is_exactly_why_not_is_none(self, ctx):
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        pairs = [
            (GUEST, ECHO_A),
            (HUMAN, ECHO_A),
            (GUEST, ECHO_B),
            (ECHO_A, ECHO_B),
            (GUEST, GUEST),
            (GUEST, HUMAN),
        ]
        for actor, peer in pairs:
            for scope in Scope:
                for via in ("direct", "group:conv-x"):
                    assert g.can(actor, peer, scope, via=via) is (
                        g.why_not(actor, peer, scope, via=via) is None
                    ), (actor, peer, scope, via)

    def test_refusal_reason_is_specific(self, ctx):
        g = ctx["graph"]
        # 非好友
        assert g.why_not(GUEST, ECHO_A, Scope.CHAT)["reason"] == RefusalReason.NOT_FRIEND.value
        # 好友但没给 delegate
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        why = g.why_not(GUEST, ECHO_A, Scope.DELEGATE)
        assert why["reason"] == RefusalReason.MISSING_SCOPE.value
        assert why["needScope"] == "delegate"
        assert "peek" in why["granted"]

    def test_group_boundary_has_its_own_reason(self, ctx):
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A, scopes=["peek", "chat", "invite", "delegate"])
        assert g.can(GUEST, ECHO_A, Scope.DELEGATE) is True
        why = g.why_not(GUEST, ECHO_A, Scope.DELEGATE, via="group:conv-1")
        assert why["reason"] == RefusalReason.GROUP_BOUNDARY.value

    def test_refusal_data_always_carries_a_hint(self, ctx):
        g = ctx["graph"]
        data = g.refusal_data(GUEST, ECHO_A, Scope.DELEGATE)
        assert data["reason"] == RefusalReason.NOT_FRIEND.value
        assert data["peer"] == ECHO_A
        # 拒绝要能指路：hint 里必须给出下一步动作
        assert "POST /social/requests" in data["hint"]

    def test_blocked_reason_never_says_the_word_blocked(self, ctx):
        """被拉黑时对外措辞必须中性——说破等于泄露信息。"""
        g = ctx["graph"]
        g.block(HUMAN, GUEST)
        why = g.why_not(GUEST, HUMAN, Scope.CHAT)
        assert why["reason"] == RefusalReason.BLOCKED.value
        assert "拉黑" not in why["detail"]
        assert "blocked" not in why["detail"]


# --------------------------------------------------------------------------- #
# A2A 任务层：delegate 门禁
# --------------------------------------------------------------------------- #


class TestDelegateGate:
    async def test_stranger_cannot_run_a_task(self, ctx):
        resp = await _send(ctx["disp"], "echo-a")
        assert "error" in resp
        assert resp["error"]["code"] == JsonRpcErrorCodes.SOCIAL_DENIED
        data = resp["error"]["data"]
        assert data["needScope"] == "delegate"
        assert data["reason"] == RefusalReason.NOT_FRIEND.value
        assert data["hint"]

    async def test_friend_without_delegate_is_still_refused(self, ctx):
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想用你的能力")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)  # 只给默认对话档
        assert g.can(GUEST, ECHO_A, Scope.CHAT) is True
        resp = await _send(ctx["disp"], "echo-a")
        assert resp["error"]["code"] == JsonRpcErrorCodes.SOCIAL_DENIED
        assert resp["error"]["data"]["reason"] == RefusalReason.MISSING_SCOPE.value

    async def test_delegate_grant_unlocks_execution(self, ctx):
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想用你的能力")
        g.accept(
            HUMAN, GUEST, as_member=ECHO_A,
            scopes=["peek", "chat", "invite", "delegate"],
        )
        resp = await _send(ctx["disp"], "echo-a")
        assert "error" not in resp, resp
        assert resp["result"]["status"]["state"] == "completed"

    async def test_delegate_is_directional(self, ctx):
        """A 给 B 执行权 ≠ B 给 A 执行权。这是权限模型里最容易搞反的一条。"""
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(
            HUMAN, GUEST, as_member=ECHO_A,
            scopes=["peek", "chat", "invite", "delegate"],
        )
        assert g.can(GUEST, ECHO_A, Scope.DELEGATE) is True
        assert g.can(ECHO_A, GUEST, Scope.DELEGATE) is False

    async def test_owner_can_always_run_own_agent(self, ctx):
        resp = await _send(ctx["disp"], "echo-a", actor=HUMAN)
        assert "error" not in resp

    async def test_message_stream_is_gated_too(self, ctx):
        """流式入口不能被漏掉——它和 message/send 是同一个执行面。

        注意 ``message/stream`` 返回的是异步生成器，异常要到**迭代时**才抛，
        所以这里必须真的把流消费掉才算验证过。
        """
        gen = await ctx["disp"].handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "message/stream",
                "params": {"agentId": "echo-a", "message": "你好"},
            },
            actor=GUEST,
        )
        from a2a_hub.models import A2AError

        with pytest.raises(A2AError) as err:
            async for _ in gen:
                pass
        assert err.value.code == JsonRpcErrorCodes.SOCIAL_DENIED

    async def test_no_graph_means_everything_is_open(self, dispatcher):
        """退化行为：没有 members.yaml 时与 v0.3.0 逐字节一致。"""
        resp = await _send(dispatcher, "echo-a", actor="user")
        assert "error" not in resp
        assert resp["result"]["status"]["state"] == "completed"


# --------------------------------------------------------------------------- #
# 编排层：门禁在选人时生效
# --------------------------------------------------------------------------- #


class TestOrchestratorGate:
    def test_named_agents_are_checked(self, ctx):
        g, orch = ctx["graph"], ctx["orch"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A, scopes=["peek", "chat", "delegate"])
        run = orch.create_run("delegate", "写点东西", ["echo-a", "echo-b"], actor=GUEST)
        # 显式点名时：派得动的留下，派不动的记下——但不影响可派的那些
        picked = orch._pick_agents(run, run.prompt, default_k=1)
        assert [r.id for r in picked] == ["echo-a"]
        assert run.refusedAgents == ["echo-b"]

    def test_all_named_agents_refused_fails_loudly(self, ctx):
        """点名的全派不动 → 明确报错，不要空跑一轮让人猜。"""
        g, orch = ctx["graph"], ctx["orch"]
        g.request(GUEST, ECHO_B, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_B)  # 只给对话档
        run = orch.create_run("delegate", "写点东西", ["echo-b"], actor=GUEST)
        with pytest.raises(SocialNotPermitted):
            orch._pick_agents(run, run.prompt, default_k=1)

    def test_auto_route_filters_out_the_unreachable(self, ctx):
        """自动路由不能把活派给「不是好友」的 agent。"""
        g, orch = ctx["graph"], ctx["orch"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(
            HUMAN, GUEST, as_member=ECHO_A,
            scopes=["peek", "chat", "invite", "delegate"],
        )
        run = orch.create_run("broadcast", "随便问点什么", [], actor=GUEST)
        picked = orch._pick_agents(run, run.prompt, default_k=3)
        assert [r.id for r in picked] == ["echo-a"]
        assert "echo-b" in run.refusedAgents

    async def test_explicit_pipeline_stage_is_checked(self, ctx):
        """流水线可以绕过 ``_pick_agents``（显式 stages），所以它必须自己过门禁。"""
        orch = ctx["orch"]
        run = orch.create_run(
            "pipeline",
            "做件事",
            [],
            {"stages": [{"agent": "echo-b", "label": "下游"}]},
            actor=GUEST,
        )
        with pytest.raises(SocialNotPermitted):
            await orch._run_pipeline(run)

    def test_unrefused_agents_pass_the_check(self, ctx):
        orch = ctx["orch"]
        run = orch.create_run("broadcast", "x", actor=HUMAN)
        assert orch._can_delegate(run, "echo-b") is True


# --------------------------------------------------------------------------- #
# 群聊边界
# --------------------------------------------------------------------------- #


class TestGroupBoundary:
    async def test_creating_a_group_requires_invite(self, ctx):
        hub = ctx["hub"]
        with pytest.raises(PermissionError):
            hub.create_group(["echo-a"], actor=GUEST)

    async def test_owner_can_group_own_agents(self, ctx):
        hub = ctx["hub"]
        conv = hub.create_group(["echo-a", "echo-b"], actor=HUMAN)
        assert conv.kind == "group"
        assert conv.members == ["echo-a", "echo-b"]

    async def test_group_membership_does_not_grant_execution(self, ctx):
        """**最关键的一条**：同群可以聊，但不等于能驱动对方的 agent。"""
        g = ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A, scopes=["peek", "chat", "invite"])
        assert g.can(GUEST, ECHO_A, Scope.CHAT, via="group:g1") is True
        for s in (Scope.DELEGATE, Scope.ARTIFACT, Scope.PROFILE):
            assert g.can(GUEST, ECHO_A, s, via="group:g1") is False

    async def test_group_chat_still_works_for_friends(self, ctx):
        hub, g = ctx["hub"], ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A, scopes=["peek", "chat", "invite"])
        conv = hub.create_group(["echo-a"], actor=GUEST)  # 有 invite，可以建
        res = await hub.send(conv.id, "大家好", sender=GUEST)
        assert res["woke"] == ["echo-a"]

    async def test_non_owner_cannot_reshuffle_a_group(self, ctx):
        hub, g = ctx["hub"], ctx["graph"]
        conv = hub.create_group(["echo-a"], actor=HUMAN)
        with pytest.raises(PermissionError):
            hub.update_group(conv.id, add=["echo-b"], actor=GUEST)

    async def test_group_is_visible_to_its_agent_members(self, ctx):
        """members 里是裸 agent id，viewer 可能是带前缀的成员 id——两边都得认。"""
        hub = ctx["hub"]
        conv = hub.create_group(["echo-a"], actor=HUMAN)
        assert hub.can_view(conv, HUMAN) is True
        assert hub.can_view(conv, ECHO_A) is True


# --------------------------------------------------------------------------- #
# 关系解除 → 会话归档
# --------------------------------------------------------------------------- #


class TestFrozenConversations:
    async def test_revoke_freezes_the_direct_chat(self, ctx):
        hub, g = ctx["hub"], ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        conv = hub.open_direct("echo-a", owner=GUEST)
        assert conv.frozen is False
        hub.freeze_between(GUEST, ECHO_A)
        assert hub.get(conv.id).frozen is True
        with pytest.raises(ValueError, match="归档"):
            await hub.send(conv.id, "还在吗", sender=GUEST)

    async def test_history_stays_readable_when_frozen(self, ctx):
        """归档不是删除——审计链不能断。"""
        hub, g = ctx["hub"], ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        conv = hub.open_direct("echo-a", owner=GUEST)
        await hub.send(conv.id, "先说一句", sender=GUEST)
        hub.freeze_between(GUEST, ECHO_A)
        history = hub.history(conv.id, viewer=GUEST)
        assert any(m["text"] == "先说一句" for m in history["messages"])

    async def test_refriending_thaws_the_chat(self, ctx):
        """重新加回好友必须能说话。

        少了 thaw，``open_direct`` 的幂等性会返回那个仍然归档的会话，
        于是「加回来了却发不出消息」——一个极难自查的死结。
        """
        hub, g = ctx["hub"], ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        conv = hub.open_direct("echo-a", owner=GUEST)
        hub.freeze_between(GUEST, ECHO_A)
        g.revoke(GUEST, ECHO_A)
        g.request(GUEST, ECHO_A, "再聊聊")
        g.accept(HUMAN, GUEST, as_member=ECHO_A)
        hub.thaw_between(GUEST, ECHO_A)
        assert hub.get(conv.id).frozen is False
        res = await hub.send(conv.id, "我又来了", sender=GUEST)
        assert res["woke"] == ["echo-a"]

    async def test_refusal_note_explains_which_kind_of_refusal(self, ctx):
        """三种拒绝（非好友 / 好友没给 chat / 群边界）要做的事完全不同。"""
        hub, g = ctx["hub"], ctx["graph"]
        conv = hub.open_direct("echo-a", owner=GUEST)
        res = await hub.send(conv.id, "在吗", sender=GUEST)
        assert res["refused"] == ["echo-a"]
        note = [m for m in hub.get(conv.id).messages if m.kind == "system"][-1]
        assert "尚未建立好友关系" in note.text

    async def test_refusal_note_names_the_missing_scope_case(self, ctx):
        hub, g = ctx["hub"], ctx["graph"]
        g.request(GUEST, ECHO_A, "想加你")
        g.accept(HUMAN, GUEST, as_member=ECHO_A, scopes=[])  # 好友但一档都不给
        conv = hub.open_direct("echo-a", owner=GUEST)
        res = await hub.send(conv.id, "在吗", sender=GUEST)
        assert res["refused"] == ["echo-a"]
        note = [m for m in hub.get(conv.id).messages if m.kind == "system"][-1]
        assert "是好友" in note.text and "chat" in note.text


# --------------------------------------------------------------------------- #
# A2A 协议兼容：错误码与声明
# --------------------------------------------------------------------------- #


class TestA2ACompat:
    def test_social_denied_code_is_registered(self):
        assert JsonRpcErrorCodes.SOCIAL_DENIED == -32008

    def test_group_scopes_are_a_strict_subset(self):
        """群聊通道允许的档位必须严格小于「好友默认档」——否则边界形同虚设。"""
        assert set(GROUP_SCOPES) == {Scope.PEEK, Scope.CHAT}
        assert Scope.DELEGATE not in GROUP_SCOPES
        assert Scope.ARTIFACT not in GROUP_SCOPES
