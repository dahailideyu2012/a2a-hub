"""SQLite persistence and restart recovery tests for the IM layer."""

from __future__ import annotations

from a2a_hub.social import (
    ChatMessage,
    Conversation,
    Delivery,
    DeliveryState,
    SocialHub,
    USER,
)
from a2a_hub.social_store import SqliteSocialStore


async def test_restart_restores_conversations_messages_receipts_and_reads(
    registry, tmp_path
):
    path = tmp_path / "social.db"
    first = SocialHub(registry, registry.bus, persistence_path=path)
    conv = first.open_direct("echo-a", owner="human:owner")
    sent = await first.send(conv.id, "持久化消息", sender="human:owner", wake=[])
    reply = ChatMessage(
        conversationId=conv.id,
        sender="echo-a",
        senderName="回显 A",
        text="持久化回复",
        replyTo=sent["message"]["id"],
        taskId="task-persisted",
    )
    conv.messages.append(reply)
    conv.deliveries.append(
        Delivery(
            messageId=sent["message"]["id"],
            agentId="echo-a",
            agentName="回显 A",
            state=DeliveryState.REPLIED,
            taskId="task-persisted",
        )
    )
    first.mark_read(conv.id, "human:owner")
    original_id = conv.id
    original_context = conv.contextId
    await first.aclose()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    restored = recovered.get(original_id)
    assert restored.contextId == original_context
    assert [m.text for m in restored.messages] == ["持久化消息", "持久化回复"]
    assert restored.deliveries[0].state is DeliveryState.REPLIED
    assert restored.deliveries[0].taskId == "task-persisted"
    assert restored.reads["human:owner"] == restored.messages[-1].id
    assert recovered.open_direct("echo-a", owner="human:owner").id == original_id
    await recovered.aclose()


async def test_restart_marks_all_inflight_states_failed_and_preserves_terminal(
    registry, tmp_path
):
    path = tmp_path / "social.db"
    first = SocialHub(registry, registry.bus, persistence_path=path)
    conv = first.open_direct("echo-a")
    message = ChatMessage(conversationId=conv.id, sender=USER, text="处理中")
    conv.messages.append(message)
    states = [
        DeliveryState.PENDING,
        DeliveryState.DELIVERED,
        DeliveryState.READ,
        DeliveryState.REPLIED,
        DeliveryState.FAILED,
    ]
    for index, state in enumerate(states):
        conv.deliveries.append(
            Delivery(
                messageId=message.id,
                agentId=f"echo-{index}",
                agentName=f"Agent {index}",
                state=state,
                taskId=f"task-{index}",
            )
        )
    first._publish(conv, "delivery")
    conv_id = conv.id
    await first.aclose()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    restored = recovered.get(conv_id)
    assert [d.state for d in restored.deliveries] == [
        DeliveryState.FAILED,
        DeliveryState.FAILED,
        DeliveryState.FAILED,
        DeliveryState.REPLIED,
        DeliveryState.FAILED,
    ]
    assert all("restarted" in (d.error or "") for d in restored.deliveries[:3])
    assert any("因 Hub 重启中断" in item.text for item in restored.messages)
    await recovered.aclose()


async def test_startup_trims_over_500_rows_and_normalizes_order(registry, tmp_path):
    path = tmp_path / "social.db"
    store = SqliteSocialStore(path)
    for index in range(505):
        conv = Conversation(id=f"conv-{index:03d}", kind="group", title=f"Group {index}")
        store.save(conv.id, index, conv.updatedAt, conv.model_dump_json())
    store.close()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    assert len(recovered._order) == 500
    assert recovered._order[0] == "conv-005"
    assert recovered._order[-1] == "conv-504"
    assert not recovered.has("conv-000")
    await recovered.aclose()

    verify = SqliteSocialStore(path)
    rows = verify._conn.execute(
        "SELECT id, position FROM social_conversations ORDER BY position"
    ).fetchall()
    verify.close()
    assert len(rows) == 500
    assert rows[0] == ("conv-005", 0)
    assert rows[-1] == ("conv-504", 499)


async def test_disband_removes_persisted_conversation(registry, tmp_path):
    path = tmp_path / "social.db"
    first = SocialHub(registry, registry.bus, persistence_path=path)
    conv = first.open_direct("echo-a")
    conv_id = conv.id
    assert first.disband(conv_id) is True
    await first.aclose()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    assert recovered.has(conv_id) is False
    await recovered.aclose()


async def test_conversation_order_survives_disband_and_restart(registry, tmp_path):
    path = tmp_path / "social.db"
    first = SocialHub(registry, registry.bus, persistence_path=path)
    oldest = first.open_direct("echo-a")
    newest = first.open_direct("echo-b")
    assert first.disband(oldest.id) is True
    latest = first.create_group(["echo-a"], title="latest")
    await first.aclose()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    assert [item["id"] for item in recovered.list_conversations(USER)] == [
        latest.id,
        newest.id,
    ]
    await recovered.aclose()


async def test_runtime_registration_evicts_oldest_and_keeps_500_rows(registry, tmp_path):
    path = tmp_path / "social.db"
    social = SocialHub(registry, registry.bus, persistence_path=path)

    created = []
    for index in range(501):
        conv = social.create_group(["echo-a"], title=f"runtime-{index:03d}")
        created.append(conv.id)

    assert len(social._order) == 500
    assert len(social._convs) == 500
    assert created[0] not in social._convs
    assert created[-1] in social._convs

    rows = social._store._conn.execute(
        "SELECT id, position FROM social_conversations ORDER BY position"
    ).fetchall()
    assert len(rows) == 500
    assert rows[0] == (created[1], 0)
    assert rows[-1] == (created[-1], 499)
    assert [row[0] for row in rows] == social._order
    await social.aclose()


async def test_sqlite_store_write_failures_are_raised_and_recoverable(tmp_path):
    import sqlite3

    store = SqliteSocialStore(tmp_path / "write-failure.db")
    now = "2026-11-23T00:00:00Z"
    store._conn.execute(
        """
        CREATE TRIGGER fail_social_insert
        BEFORE INSERT ON social_conversations
        BEGIN
            SELECT RAISE(ABORT, 'injected insert failure');
        END;
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        store.save("conv-fail", 0, now, "{}")
    store._conn.execute("DROP TRIGGER fail_social_insert")
    store.save("conv-ok", 0, now, "{}")

    store._conn.execute(
        """
        CREATE TRIGGER fail_social_update
        BEFORE UPDATE ON social_conversations
        BEGIN
            SELECT RAISE(ABORT, 'injected update failure');
        END;
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected update failure"):
        store.reorder(["conv-ok"])
    store._conn.execute("DROP TRIGGER fail_social_update")

    store._conn.execute(
        """
        CREATE TRIGGER fail_social_delete
        BEFORE DELETE ON social_conversations
        BEGIN
            SELECT RAISE(ABORT, 'injected delete failure');
        END;
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected delete failure"):
        store.delete("conv-ok")
    store._conn.execute("DROP TRIGGER fail_social_delete")

    assert store.load() == ["{}"]
    store.delete("conv-ok")
    assert store.load() == []
    store.close()
