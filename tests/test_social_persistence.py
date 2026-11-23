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


async def test_restart_marks_inflight_delivery_failed_and_visible(registry, tmp_path):
    path = tmp_path / "social.db"
    first = SocialHub(registry, registry.bus, persistence_path=path)
    conv = first.open_direct("echo-a")
    message = ChatMessage(conversationId=conv.id, sender=USER, text="处理中")
    conv.messages.append(message)
    conv.deliveries.append(
        Delivery(
            messageId=message.id,
            agentId="echo-a",
            agentName="回显 A",
            state=DeliveryState.READ,
            taskId="task-interrupted",
        )
    )
    first._publish(conv, "delivery")
    conv_id = conv.id
    await first.aclose()

    recovered = SocialHub(registry, registry.bus, persistence_path=path)
    restored = recovered.get(conv_id)
    assert restored.deliveries[0].state is DeliveryState.FAILED
    assert "restarted" in (restored.deliveries[0].error or "")
    assert any("因 Hub 重启中断" in item.text for item in restored.messages)
    await recovered.aclose()


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
