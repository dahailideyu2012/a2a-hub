"""会话层（IM）测试。

覆盖「像微信一样沟通」的核心承诺：
  * 单聊发出去就有回复，且**上下文自动延续**
  * 群聊 @ 谁谁才说话；没人被 @ 时按能力挑人，可关闭
  * 每条消息对每位 agent 都有独立的投递回执（已送达 / 已读 / 已回复 / 失败）
  * 会话内所有 Task 共享同一个 contextId
  * 失败必须看得见，不能静默消失
"""

from __future__ import annotations

import pytest

from a2a_hub.social import USER, ChatMessage, DeliveryState, SocialHub

# 注意：pyproject 里 asyncio_mode = "auto"，异步用例无需显式标记；
# 这里刻意不加模块级标记，以免影响 TestHttpApi 里的同步用例。


# --------------------------------------------------------------------------- #
# 通讯录
# --------------------------------------------------------------------------- #


class TestContacts:
    async def test_lists_registered_agents(self, social: SocialHub):
        items = social.contacts()
        ids = {i["id"] for i in items}
        assert {"echo-a", "echo-b", "static-card"} <= ids
        for item in items:
            assert {"id", "name", "online", "status", "skills"} <= set(item)

    async def test_disabled_agents_are_hidden(self, social: SocialHub, registry):
        registry.get("echo-b").spec.enabled = False
        assert "echo-b" not in {i["id"] for i in social.contacts()}


# --------------------------------------------------------------------------- #
# 单聊
# --------------------------------------------------------------------------- #


class TestDirectChat:
    async def test_open_direct_is_idempotent(self, social: SocialHub):
        a = social.open_direct("echo-a")
        b = social.open_direct("echo-a")
        assert a.id == b.id
        assert a.members == ["echo-a"]

    async def test_send_returns_immediately_with_pending_delivery(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        res = await social.send(conv.id, "你好")
        # 关键：发消息不等回复，立刻返回
        assert res["woke"] == ["echo-a"]
        assert res["message"]["text"] == "你好"
        assert len(res["deliveries"]) == 1

    async def test_agent_replies_as_a_new_message(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "你好")
        await social.wait_idle(conv.id, timeout=15)

        h = social.history(conv.id)
        assert [m["sender"] for m in h["messages"]] == [USER, "echo-a"]
        assert h["messages"][1]["replyTo"] == h["messages"][0]["id"]
        assert "[A]" in h["messages"][1]["text"]

    async def test_delivery_reaches_replied(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "你好")
        await social.wait_idle(conv.id, timeout=15)

        d = social.get(conv.id).deliveries[0]
        assert d.state is DeliveryState.REPLIED
        assert d.taskId is not None
        assert d.is_terminal

    async def test_same_agent_in_two_conversations_is_isolated(self, social: SocialHub):
        c1 = social.open_direct("echo-a")
        c2 = social.create_group(["echo-a"], "另一个场子")
        await social.send(c1.id, "只在这里说")
        await social.wait_idle(c1.id, timeout=15)

        assert len(social.get(c1.id).messages) == 2
        assert len(social.get(c2.id).messages) == 0
        assert c1.contextId != c2.contextId


# --------------------------------------------------------------------------- #
# @ 提及与群聊唤醒
# --------------------------------------------------------------------------- #


class TestMentions:
    def test_parse_mention_by_id(self, social: SocialHub):
        ids, broadcast = social.parse_mentions("请 @echo-a 看一下")
        assert ids == ["echo-a"]
        assert broadcast is False

    def test_parse_mention_by_name_prefix(self, social: SocialHub):
        # 「会失败的 Agent」带空格，正则只能抓到「会失败」，靠前缀匹配兜底
        ids, _ = social.parse_mentions("@会失败 你在吗")
        assert ids == ["echo-fail"]

    def test_ambiguous_prefix_is_ignored(self, social: SocialHub):
        # echo-a 和 echo-b 都叫「回显 X」，@回显 指代不明 —— 宁可当没 @ 过
        ids, _ = social.parse_mentions("@回显 帮个忙")
        assert ids == []

    def test_parse_broadcast(self, social: SocialHub):
        ids, broadcast = social.parse_mentions("@所有人 汇报一下")
        assert ids == []
        assert broadcast is True

    def test_unknown_mention_is_ignored(self, social: SocialHub):
        ids, broadcast = social.parse_mentions("联系 foo@example.com 即可")
        assert ids == []
        assert broadcast is False


class TestGroupChat:
    async def test_mention_wakes_only_the_target(self, social: SocialHub):
        conv = social.create_group(["echo-a", "echo-b"], "测试群")
        res = await social.send(conv.id, "@echo-b 你来")
        assert res["woke"] == ["echo-b"]

        await social.wait_idle(conv.id, timeout=15)
        h = social.history(conv.id)
        assert [d["agentId"] for d in h["deliveries"]] == ["echo-b"]

    async def test_broadcast_wakes_everyone(self, social: SocialHub):
        conv = social.create_group(["echo-a", "echo-b"], "测试群")
        res = await social.send(conv.id, "@所有人 各说一句")
        assert set(res["woke"]) == {"echo-a", "echo-b"}

        await social.wait_idle(conv.id, timeout=20)
        assert len(social.get(conv.id).deliveries) == 2
        assert all(d.state is DeliveryState.REPLIED for d in social.get(conv.id).deliveries)

    async def test_no_mention_autoroutes_to_best_match(self, social: SocialHub):
        conv = social.create_group(["echo-a", "echo-b"], "测试群")
        # echo-a 有 code-review 技能，echo-b 是写作
        res = await social.send(conv.id, "帮我评审这段代码")
        assert res["woke"] == ["echo-a"]

    async def test_autoroute_can_be_disabled(self, social: SocialHub):
        conv = social.create_group(["echo-a"], "静默群", auto_route=False)
        res = await social.send(conv.id, "有人吗")
        assert res["woke"] == []
        assert res["deliveries"] == []

    async def test_autoroute_prefers_healthy_agent(self, social: SocialHub, registry):
        # echo-a 能力更强但离线，应该让位给 echo-b
        registry.get("echo-a").health = {"status": "unavailable", "detail": "测试标记离线"}
        conv = social.create_group(["echo-a", "echo-b"], "测试群")
        res = await social.send(conv.id, "帮我评审这段代码")
        assert res["woke"] == ["echo-b"]

    async def test_wake_can_be_overridden(self, social: SocialHub):
        conv = social.create_group(["echo-a", "echo-b"], "测试群")
        res = await social.send(conv.id, "没人被 @ 的一段话", wake=["echo-b"])
        assert res["woke"] == ["echo-b"]

    async def test_explicit_wake_empty_means_archived_only(self, social: SocialHub):
        conv = social.create_group(["echo-a"], "测试群")
        res = await social.send(conv.id, "只存档不要打扰", wake=[])
        assert res["woke"] == []
        assert len(social.get(conv.id).messages) == 1


class TestGroupMembership:
    async def test_add_and_remove_members(self, social: SocialHub):
        conv = social.create_group(["echo-a"], "测试群")
        social.update_group(conv.id, add=["echo-b"])
        assert conv.members == ["echo-a", "echo-b"]
        # 成员变动会留下系统消息
        assert any(m.kind == "system" for m in conv.messages)

        social.update_group(conv.id, remove=["echo-a"])
        assert conv.members == ["echo-b"]

    async def test_unknown_member_rejected(self, social: SocialHub):
        with pytest.raises(Exception):
            social.create_group(["no-such-agent"], "无效群")

    async def test_cannot_update_direct_chat(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        with pytest.raises(ValueError):
            social.update_group(conv.id, add=["echo-b"])

    async def test_disband(self, social: SocialHub):
        conv = social.create_group(["echo-a"], "临时群")
        assert social.disband(conv.id) is True
        assert social.has(conv.id) is False
        assert social.disband(conv.id) is False


# --------------------------------------------------------------------------- #
# 上下文延续（这一层存在的理由）
# --------------------------------------------------------------------------- #


class TestContext:
    async def test_tasks_share_conversation_context(self, social: SocialHub, registry):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "第一句")
        await social.wait_idle(conv.id, timeout=15)
        await social.send(conv.id, "第二句")
        await social.wait_idle(conv.id, timeout=15)

        ctxs = {
            registry.store.get(m.taskId).contextId
            for m in social.get(conv.id).messages
            if m.taskId
        }
        assert ctxs == {conv.contextId}

    def test_prompt_carries_history_and_puts_current_last(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        first = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="第一句")
        second = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="第二句")
        conv.messages.extend([first, second])

        prompt = social._render_prompt(conv, second, "echo-a")
        assert "第一句" in prompt  # 带上了上文
        assert prompt.rstrip().endswith("第二句")  # 当前消息在最后

    def test_group_prompt_announces_the_scene(self, social: SocialHub):
        conv = social.create_group(["echo-a", "echo-b"], "架构组")
        first = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="开场")
        second = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="继续")
        conv.messages.extend([first, second])

        prompt = social._render_prompt(conv, second, "echo-a")
        assert "架构组" in prompt
        assert "群聊" in prompt

    def test_first_message_has_no_history_preamble(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        only = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="就这一句")
        conv.messages.append(only)
        assert social._render_prompt(conv, only, "echo-a") == "就这一句"

    def test_history_window_is_bounded(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        conv.metadata["history"] = 2
        for i in range(6):
            conv.messages.append(
                ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text=f"历史{i}")
            )
        current = ChatMessage(conversationId=conv.id, sender=USER, senderName="我", text="当前")
        conv.messages.append(current)

        prompt = social._render_prompt(conv, current, "echo-a")
        assert "历史5" in prompt and "历史4" in prompt
        assert "历史0" not in prompt  # 超出窗口的必须丢弃


# --------------------------------------------------------------------------- #
# 回执与未读
# --------------------------------------------------------------------------- #


class TestReadReceipts:
    async def test_unread_then_mark_read(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "你好")
        await social.wait_idle(conv.id, timeout=15)

        assert social.unread_count(social.get(conv.id), USER) == 1
        social.mark_read(conv.id, USER)
        assert social.unread_count(social.get(conv.id), USER) == 0

    async def test_sender_own_message_is_not_unread(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "只有我说话")
        assert social.unread_count(social.get(conv.id), USER) == 0

    async def test_delivered_agent_marks_its_own_read(self, social: SocialHub, registry):
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "你好")
        await social.wait_idle(conv.id, timeout=15)
        # agent 一旦被投递，就视为已读——红点不该停留在它头上
        assert social.unread_count(social.get(conv.id), "echo-a") == 0


# --------------------------------------------------------------------------- #
# 失败必须可见
# --------------------------------------------------------------------------- #


class TestFailureVisibility:
    async def test_agent_error_becomes_visible_delivery_and_system_note(
        self, social: SocialHub
    ):
        conv = social.open_direct("echo-fail")
        await social.send(conv.id, "BOOM")
        await social.wait_idle(conv.id, timeout=15)

        d = social.get(conv.id).deliveries[0]
        assert d.state is DeliveryState.FAILED
        assert d.error

        notes = [m for m in social.get(conv.id).messages if m.kind == "system"]
        assert len(notes) == 1
        assert "echo-fail" in notes[0].text or "会失败的 Agent" in notes[0].text

    async def test_unavailable_agent_fails_fast(self, social: SocialHub, registry):
        registry.get("echo-a").health = {"status": "unavailable", "detail": "测试标记离线"}
        conv = social.open_direct("echo-a")
        await social.send(conv.id, "你好")
        await social.wait_idle(conv.id, timeout=5)

        d = social.get(conv.id).deliveries[0]
        assert d.state is DeliveryState.FAILED
        assert "不可用" in (d.error or "")

    async def test_empty_message_rejected(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        with pytest.raises(ValueError):
            await social.send(conv.id, "   ")

    async def test_unknown_conversation_raises(self, social: SocialHub):
        with pytest.raises(KeyError):
            await social.send("conv-does-not-exist", "你好")


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #


class TestEventStream:
    async def test_stream_starts_with_snapshot(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        agen = social.stream(conv.id)
        first = await agen.__anext__()
        assert first["event"] == "snapshot"
        assert first["conversationId"] == conv.id
        await agen.aclose()

    async def test_message_event_is_published(self, social: SocialHub):
        conv = social.open_direct("echo-a")
        q = social.bus.subscribe(social._channel(conv.id))

        await social.send(conv.id, "你好")
        event = q.get_nowait()
        assert event["event"] == "message"
        assert event["message"]["text"] == "你好"

        await social.wait_idle(conv.id, timeout=15)
        social.bus.unsubscribe(social._channel(conv.id), q)


# --------------------------------------------------------------------------- #
# 对外接口：HTTP 与 JSON-RPC
# --------------------------------------------------------------------------- #


class TestHttpApi:
    def test_contacts_endpoint(self, client):
        r = client.get("/im/contacts")
        assert r.status_code == 200
        assert r.json()["count"] >= 3

    def test_create_direct_and_send(self, client):
        r = client.post("/im/conversations", json={"kind": "direct", "agent": "echo-a"})
        assert r.status_code == 200
        cid = r.json()["id"]
        assert r.json()["kind"] == "direct"

        r = client.post(f"/im/conversations/{cid}/messages", json={"text": "你好"})
        assert r.status_code == 200
        assert r.json()["woke"] == ["echo-a"]

        assert client.get(f"/im/conversations/{cid}").status_code == 200
        assert client.get("/im/conversations").json()["count"] >= 1

    def test_create_group_and_mention(self, client):
        r = client.post(
            "/im/conversations",
            json={"kind": "group", "members": ["echo-a", "echo-b"], "title": "端到端群"},
        )
        assert r.status_code == 200
        cid = r.json()["id"]

        r = client.post(f"/im/conversations/{cid}/messages", json={"text": "@echo-b 你来"})
        assert r.json()["woke"] == ["echo-b"]

    def test_unknown_conversation_is_404(self, client):
        assert client.get("/im/conversations/conv-nope").status_code == 404
        r = client.post("/im/conversations/conv-nope/messages", json={"text": "hi"})
        assert r.status_code == 404

    def test_unknown_agent_is_404(self, client):
        r = client.post("/im/conversations", json={"kind": "direct", "agent": "ghost"})
        assert r.status_code == 404

    def test_empty_message_is_422(self, client):
        cid = client.post(
            "/im/conversations", json={"kind": "direct", "agent": "echo-a"}
        ).json()["id"]
        r = client.post(f"/im/conversations/{cid}/messages", json={"text": "  "})
        assert r.status_code == 422

    def test_mark_read_and_disband(self, client):
        cid = client.post(
            "/im/conversations", json={"kind": "direct", "agent": "echo-a"}
        ).json()["id"]
        assert client.post(f"/im/conversations/{cid}/read", json={}).status_code == 200
        assert client.delete(f"/im/conversations/{cid}").json()["disbanded"] is True
        assert client.get(f"/im/conversations/{cid}").status_code == 404


class TestRpcMethods:
    async def test_im_methods_are_registered(self):
        from a2a_hub.rpc import JsonRpcDispatcher

        methods = JsonRpcDispatcher.supported_methods()
        for m in ("im/contacts", "im/conversations", "im/open", "im/group",
                  "im/send", "im/history", "im/events"):
            assert m in methods

    async def test_open_send_history_roundtrip(self, dispatcher):
        res = await dispatcher.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "im/open", "params": {"agentId": "echo-a"}}
        )
        assert "result" in res, res
        cid = res["result"]["id"]

        res = await dispatcher.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "im/send",
                "params": {"conversationId": cid, "text": "你好"},
            }
        )
        assert res["result"]["woke"] == ["echo-a"]

        res = await dispatcher.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "im/history",
                "params": {"conversationId": cid},
            }
        )
        assert res["result"]["messages"][0]["text"] == "你好"

    async def test_group_method(self, dispatcher):
        res = await dispatcher.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "im/group",
                "params": {"members": ["echo-a", "echo-b"], "title": "RPC 群"},
            }
        )
        assert res["result"]["kind"] == "group"
        assert len(res["result"]["members"]) == 2

    async def test_unknown_conversation_maps_to_invalid_params(self, dispatcher):
        res = await dispatcher.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "im/send",
                "params": {"conversationId": "conv-nope", "text": "hi"},
            }
        )
        assert res["error"]["code"] == -32602

    async def test_missing_params_rejected(self, dispatcher):
        res = await dispatcher.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "im/send", "params": {}}
        )
        assert res["error"]["code"] == -32602
