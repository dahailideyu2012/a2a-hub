"""会话层（IM）—— 让 agent 之间像微信好友一样对话与协作。

为什么需要单独一层
------------------
A2A 协议里的 ``Task`` 是**执行单位**：提交一次、跑到终态、结束即亡。
而人类协作是**持续会话**，两者的语义根本不同：

    A2A / Task 语义          微信 / IM 语义
    ─────────────────       ──────────────────────
    提交请求                 发一条消息
    等待返回（阻塞）         消息立刻出现在聊天窗，不等对方
    返回结果 = 请求结束      对方回了一条新消息（会话仍在继续）
    下次调用是全新请求       上文自动延续，"记得"之前聊过什么
    只有请求-响应两方        单聊 / 群聊 / @某人 / 已读回执 / 未读红点

中间差着一整层。本模块补齐的就是这层：

    Conversation ──> contextId ──> 复用为会话内所有 Task 的 contextId
                                   （agent 因此记得这个会话说过什么）
    ChatMessage  ──> 触发 Task ──> 产出 ──> 追加为新的 ChatMessage
    Delivery     ──> 一条消息在某位 agent 处的投递状态机

设计取舍（这几条决定了它像不像微信）
------------------------------------
1. **消息与执行解耦**：``send()`` 落库后立即返回，agent 在后台跑完再"回话"。
   这是「聊天」与「RPC」的分水岭——前者不阻塞，后者必阻塞。
2. **会话是一等公民**：上下文锚在 Conversation 而不是单次 Task 上，
   所以同一个 agent 可以同时待在多个会话里且互不串味。
3. **回执按 (消息 × agent) 粒度**：群里三个 agent 各有各的已读与回复状态，
   单聊也能显示"对方正在输入…"。
4. **@ 提及唤醒**：群里只有被 @ 的才响应，否则 N 个 agent 会齐刷刷刷屏。
   没 @ 时默认按能力路由挑一个（``autoRoute``，可关）。
5. **失败要可见**：agent 不可用/报错时，以一条系统消息呈现，
   而不是让消息石沉大海——静默失败在协作场景里最致命。
6. **本层不做鉴权**，只调用注入的门禁。``SocialHub`` 拿到的
   ``guard`` 决定「谁该出现在通讯录里」「这条消息能不能投给某人」，
   而策略全在 ``relations.py``。默认注入 ``NullGuard``（全放行），
   所以无配置时行为与 v0.3.0 一致，单测里也不需要任何身份配置。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum
from typing import Any, AsyncIterator, Literal, Optional

from pydantic import BaseModel, Field

from .bus import EventBus
from .models import Message, TaskState, new_id, utc_now
from .registry import AgentRecord, AgentRegistry
#: ``USER`` / ``SYSTEM`` 这两个保留字只在 relations.py 里定义一份——
#: 两边各写一个"user"常量迟早会漂移。
from .relations import (
    SCOPE_ORDER,
    SYSTEM,
    USER,
    FriendshipGuard,
    NullGuard,
    Scope,
)

log = logging.getLogger("a2a_hub.social")

#: @ 提名的语法：``@codex`` / ``@claude-code`` / ``@千问办公`` / ``@所有人``
_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.\-]*|[\u4e00-\u9fff]+)")

#: 「@所有人」的等价写法
_BROADCAST_TOKENS = {"all", "everyone", "所有人", "全体", "大家", "全员"}

#: 默认往前带几条历史作为上下文
DEFAULT_HISTORY = 8


def _bare(ref: str) -> str:
    """``agent:codex`` → ``codex``；其余原样。

    会话的 ``members`` 用的是**裸 agent id**（A2A 语义里的执行者），
    而 ``owner`` / ``visibleTo`` 用的是**成员 id**。两种写法在同一条记录里
    并存，比较时就得两边都试——否则 agent 自己看不到自己所在的群。
    """
    return ref[len("agent:"):] if ref.startswith("agent:") else ref


class DeliveryState(str, Enum):
    """一条消息在某位 agent 处的投递状态。"""

    PENDING = "pending"  # 已唤醒，排队中
    DELIVERED = "delivered"  # 任务已创建，对方收到了
    READ = "read"  # 对方开始处理（前端可显示"正在输入…"）
    REPLIED = "replied"  # 对方已回复
    FAILED = "failed"  # 对方没接住（不可用 / 报错 / 超时）


TERMINAL_DELIVERY = {DeliveryState.REPLIED, DeliveryState.FAILED}


class ChatMessage(BaseModel):
    """一条聊天消息。"""

    id: str = Field(default_factory=lambda: new_id("im-"))
    conversationId: str = ""
    sender: str = USER
    senderName: str = "我"
    text: str = ""
    mentions: list[str] = Field(default_factory=list)
    replyTo: Optional[str] = None
    kind: Literal["text", "system"] = "text"
    taskId: Optional[str] = None
    ts: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Delivery(BaseModel):
    """(消息 × agent) 的投递记录——群聊里谁读没读、回没回，看这个。"""

    id: str = Field(default_factory=lambda: new_id("dlv-"))
    messageId: str = ""
    agentId: str = ""
    agentName: str = ""
    state: DeliveryState = DeliveryState.PENDING
    taskId: Optional[str] = None
    error: Optional[str] = None
    ts: str = Field(default_factory=utc_now)
    updatedAt: str = Field(default_factory=utc_now)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_DELIVERY


class Conversation(BaseModel):
    """一个会话：单聊或群聊。

    ``contextId`` 是它最关键的字段——会话内所有 Task 共用它，
    这是 agent「记得上文」的技术前提。

    ``owner`` / ``visibleTo`` 把会话绑到主体上。**这不是权限判定**——
    真正的判定在注入的 ``guard`` 里；这两个字段只回答「谁的会话列表里该有它」。
    ``members`` 保持 A2A 语义（执行者），人类依然不是成员。
    """

    id: str = Field(default_factory=lambda: new_id("conv-"))
    kind: Literal["direct", "group"] = "direct"
    title: str = ""
    members: list[str] = Field(default_factory=list)
    contextId: str = Field(default_factory=lambda: new_id("ctx-"))
    messages: list[ChatMessage] = Field(default_factory=list)
    deliveries: list[Delivery] = Field(default_factory=list)
    reads: dict[str, str] = Field(default_factory=dict)
    owner: str = ""  # 创建者；"" = 遗留单主体模式
    visibleTo: list[str] = Field(default_factory=list)  # [] = 所有人可见（兼容旧行为）
    frozen: bool = False  # 删除好友后的归档态：可读不可写
    createdAt: str = Field(default_factory=utc_now)
    updatedAt: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def member_ids(self) -> list[str]:
        return list(self.members)

    def last_message(self) -> Optional[ChatMessage]:
        return self.messages[-1] if self.messages else None


class SocialHub:
    """会话层主体。

    与 ``Orchestrator`` 的区别：编排器解决「一次任务怎么分工」，
    会话层解决「一群 agent 怎么持续地聊下去」。前者是有向无环的流水线，
    后者是无限延续的消息流。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        bus: EventBus,
        guard: Optional[FriendshipGuard] = None,
    ) -> None:
        self.registry = registry
        self.bus = bus
        #: 门禁。默认 ``NullGuard``（全放行）——**本层只调用它，不实现策略**，
        #: 这样会话层在单测里依然是零依赖的。
        self.guard: FriendshipGuard = guard or NullGuard()
        self._convs: dict[str, Conversation] = {}
        self._order: list[str] = []
        # 后台派发任务：必须持有引用，否则可能被 GC 掉
        self._inflight: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ #
    # 通讯录
    # ------------------------------------------------------------------ #

    def contacts(self, viewer: str = USER) -> list[dict[str, Any]]:
        """通讯录 —— registry 里的 agent，按门禁过滤后的结果。

        无 ``members.yaml`` 时 ``guard.visible_contacts()`` 返回 ``None``，
        表示「不过滤」，于是与 v0.3.0 完全一致。
        """
        allowed = self.guard.visible_contacts(viewer)
        by_agent: dict[str, str] = {}
        for conv in self._convs.values():
            if conv.kind == "direct":
                for m in conv.members:
                    by_agent[m] = conv.id

        out: list[dict[str, Any]] = []
        for rec in self.registry.list_records():
            if not rec.spec.enabled:
                continue
            if allowed is not None and rec.id not in allowed:
                continue
            status = rec.health.get("status", "unknown")
            out.append(
                {
                    "id": rec.id,
                    "name": rec.name,
                    "type": rec.type,
                    "description": rec.description,
                    "online": status != "unavailable",
                    "status": status,
                    "detail": rec.health.get("detail", ""),
                    "tags": rec.spec.tags,
                    "skills": [s.name for s in rec.adapter.skills],
                    "conversationId": by_agent.get(rec.id),
                    "scopes": self.scopes_for(viewer, rec.id),
                    "stats": {"tasks": rec.task_count, "errors": rec.error_count},
                }
            )
        return out

    def scopes_for(self, actor: str, agent_id: str) -> list[str]:
        """``actor`` 对某位 agent 实际能做什么。

        只通过 ``guard.can()`` 逐项探测，不去翻关系图——门禁接口就这么大，
        多开一个口子就多一条绕过路径。
        """
        return [
            s.value
            for s in SCOPE_ORDER
            if self.guard.can(actor, agent_id, s)
        ]

    def _display_name(self, ref: str) -> str:
        if ref in (USER, SYSTEM):
            return "我" if ref == USER else "系统"
        if self.registry.has(ref):
            return self.registry.get(ref).name
        # 成员名（人类 / 外部平台 bot / 未注册的远端 agent）
        name = self.guard.display_name(ref) if hasattr(self.guard, "display_name") else ""
        return name or ref

    def can_view(self, conv: Conversation, viewer: str) -> bool:
        """会话可见性。``visibleTo`` 为空 = 所有人可见（兼容旧行为）。

        注意这**只回答「谁的会话列表里该有它」**，不是权限判定——
        能不能发消息由 ``guard.can()`` 决定。
        """
        if not conv.visibleTo:
            return True
        if viewer == conv.owner or viewer in conv.visibleTo:
            return True
        # 群成员数组里是裸 agent id，而 viewer 可能是带前缀的成员 id
        return viewer in conv.members or _bare(viewer) in conv.members

    # ------------------------------------------------------------------ #
    # 门禁问答（只问 guard，自己不实现策略）
    # ------------------------------------------------------------------ #

    def _why(self, actor: str, peer: str, scope: Scope, *, via: str = "direct") -> Optional[dict[str, Any]]:
        """问 guard「为什么不行」。guard 没实现就返回 ``None``（不编造理由）。"""
        fn = getattr(self.guard, "why_not", None)
        if not callable(fn):
            return None
        try:
            return fn(actor, peer, scope, via=via)
        except Exception:  # noqa: BLE001  理由只是锦上添花，不该把发送搞崩
            log.debug("guard.why_not 调用失败", exc_info=True)
            return None

    def _refusal_note(self, sender: str, refused: list[str], via: str) -> str:
        """被门禁挡下时给人看的那句话。

        「还不是好友」/「是好友但没给这一档」/「群里不给执行权」三种情况，
        用户要做的事完全不同。用一句含糊的「无权限」等于没说。
        """
        names = "、".join(self._display_name(r) for r in refused)
        reasons: list[str] = []
        for r in refused:
            why = self._why(sender, r, Scope.CHAT, via=via) or {}
            reason = why.get("reason", "")
            if reason == "group_boundary":
                reasons.append("群聊通道只放宽 `chat`，执行类权限需单独授予")
            elif reason == "missing_scope":
                got = "、".join(why.get("granted") or []) or "无"
                reasons.append(f"你与对方是好友，但对方没给你 `chat`（当前只给了 {got}）")
            elif reason == "blocked":
                reasons.append("对方当前不可达")
            else:
                reasons.append("与对方尚未建立好友关系")
        return f"⚠ {names} 未收到这条消息：{'；'.join(dict.fromkeys(reasons))}"

    def _require_group_control(self, conv: Conversation, actor: str) -> None:
        """门禁启用时，只有会话所有者能改群成员。

        群是权限放大器：谁都能往群里拉人，等于谁都能扩大自己的可接触面。
        门禁关闭时（无 ``members.yaml``）保持 v0.3.0 的宽松行为。
        """
        if not getattr(self.guard, "enabled", False):
            return
        if not conv.owner or actor == conv.owner:
            return
        raise PermissionError("只有会话所有者能修改群成员")

    def _note_interaction(self, a: str, b: str) -> None:
        """告诉门禁「这两人刚成功互动过」，用于刷新信任租约（§7.4）。

        设计明写「不依赖人工操作」——所以只能由投递成功这个事实来驱动。
        同样走 ``getattr``：``NullGuard`` 没有这个方法，门禁关闭时静默跳过。

        ⚠ 名字刻意不叫 ``_touch``：本类里已有一个 ``_touch(conv)``
        （刷新 ``conv.updatedAt``），同名会把那个覆盖掉——两个同名的
        私有方法后定义者胜，症状是「发消息时 TypeError」，极难一眼看出。
        """
        fn = getattr(self.guard, "touch_interaction", None)
        if not callable(fn):
            return
        try:
            fn(a, b)
        except Exception:  # noqa: BLE001  刷新时间戳失败不该让消息发不出去
            log.debug("guard.touch_interaction 调用失败", exc_info=True)

    def request_for_need(
        self, member: str, need: str, *, reason: str = ""
    ) -> dict[str, Any]:
        """把「我需要某种能力」的诉求交给门禁（§6.1 A 主路径）。

        会话层不实现策略——它只把信号转给 guard，由关系层按策略决定
        「去申请」「挂 owner 待办」还是「什么都不做」。
        """
        fn = getattr(self.guard, "request_for_need", None)
        if not callable(fn):
            return {
                "member": member,
                "need": need,
                "action": "ignore",
                "policy": "",
                "detail": "门禁未启用，自主交友不生效",
                "candidates": [],
                "asked": None,
            }
        return fn(member, need, reason=reason)

    # ------------------------------------------------------------------ #
    # 关系解除后的会话归档
    # ------------------------------------------------------------------ #

    def freeze_between(self, a: str, b: str) -> int:
        """把 a 与 b 之间的单聊标记为归档（**可读不可写**）。

        设计 §4.3：删好友不清历史归档，但必须禁止新消息——
        否则「关系已解除却还能继续说话」等于关系根本没解除。

        返回被冻结的会话数。
        """
        return self._set_frozen(a, b, True)

    def thaw_between(self, a: str, b: str) -> int:
        """重新成为好友时解冻历史单聊。

        少了这一步会踩坑：``open_direct`` 是幂等的，会返回**同一个**归档会话，
        于是「重新加回好友却发现发不出消息」——一个很难自查的死结。
        """
        return self._set_frozen(a, b, False)

    def _set_frozen(self, a: str, b: str, frozen: bool) -> int:
        changed = 0
        for owner_ref, agent_ref in ((a, b), (b, a)):
            for conv in self._convs.values():
                if conv.kind != "direct" or conv.frozen == frozen:
                    continue
                if conv.owner == owner_ref and _bare(agent_ref) in conv.members:
                    conv.frozen = frozen
                    self._touch(conv)
                    self._publish(
                        conv, "conversation-frozen" if frozen else "conversation-thawed"
                    )
                    changed += 1
        return changed

    # ------------------------------------------------------------------ #
    # 会话管理
    # ------------------------------------------------------------------ #

    def open_direct(self, agent_id: str, owner: str = USER) -> Conversation:
        """打开与某位 agent 的单聊（幂等：同一个 owner + 同一个 agent 只有一个）。

        匹配键带上 ``owner`` 是必须的——旧实现只看 ``members == [agent_id]``，
        于是两个人各自跟 codex 单聊会命中**同一个**会话，
        不但串台，还会让 A 看到 B 的上下文。
        """
        rec = self.registry.get(agent_id)  # 不存在会抛 A2AError
        for conv in self._convs.values():
            if (
                conv.kind == "direct"
                and conv.members == [agent_id]
                and conv.owner == owner
            ):
                return conv
        conv = Conversation(
            kind="direct",
            title=rec.name,
            members=[agent_id],
            owner=owner,
            visibleTo=[owner] if owner else [],
        )
        self._register(conv)
        self._publish(conv, "conversation-created")
        return conv

    def create_group(
        self,
        member_ids: list[str],
        title: str = "",
        auto_route: bool = True,
        actor: str = USER,
    ) -> Conversation:
        """建群。成员必须是已注册的 agent，且需要 ``invite`` 权限。"""
        members: list[str] = []
        for mid in member_ids:
            rec = self.registry.get(mid)  # 校验存在
            if rec.id not in members:
                members.append(rec.id)
        if not members:
            raise ValueError("群成员不能为空")
        self._require_invite(actor, members)
        name = title.strip() or "、".join(self._display_name(m) for m in members[:3])
        if len(members) > 3 and not title.strip():
            name += f" 等 {len(members)} 人"
        conv = Conversation(
            kind="group",
            title=name,
            members=members,
            owner=actor,
            visibleTo=[actor] if actor else [],
            metadata={"autoRoute": auto_route},
        )
        self._register(conv)
        self._publish(conv, "conversation-created")
        return conv

    def update_group(
        self,
        conv_id: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
        actor: str = USER,
    ) -> Conversation:
        """拉人进群 / 移出群。拉人需要 ``invite`` 权限。"""
        conv = self.get(conv_id)
        if conv.kind != "group":
            raise ValueError("只有群聊能改成员")
        self._require_group_control(conv, actor)
        fresh = [mid for mid in (add or []) if self.registry.get(mid).id not in conv.members]
        self._require_invite(actor, fresh)
        for mid in add or []:
            rid = self.registry.get(mid).id
            if rid not in conv.members:
                conv.members.append(rid)
                self._system(conv, f"{self._display_name(rid)} 加入了群聊")
        for mid in remove or []:
            if mid in conv.members:
                conv.members.remove(mid)
                self._system(conv, f"{self._display_name(mid)} 被移出群聊")
        self._touch(conv)
        self._publish(conv, "conversation-updated")
        return conv

    def _require_invite(self, actor: str, targets: list[str]) -> None:
        """拉人进群要对方给过 ``invite``。

        门禁关闭时（无配置）不做任何校验，与 v0.3.0 一致。
        """
        refused = [
            m for m in targets if not self.guard.can(actor, m, Scope.INVITE)
        ]
        if refused:
            names = "、".join(self._display_name(m) for m in refused)
            raise PermissionError(
                f"没有邀请 {names} 的权限：对方未授予你 `invite`"
            )

    def disband(self, conv_id: str, actor: str = USER) -> bool:
        conv = self._convs.get(conv_id)
        if conv is None:
            return False
        if conv.owner and actor != conv.owner:
            # 只有会话所有者能解散它；别人最多退出（stage 2 再补「退出」语义）
            raise PermissionError("只有会话所有者能解散该会话")
        self._convs.pop(conv_id, None)
        if conv_id in self._order:
            self._order.remove(conv_id)
        self.bus.publish(self._channel(conv_id), {"kind": "im-event", "event": "conversation-disbanded", "conversationId": conv_id})
        return True

    def get(self, conv_id: str) -> Conversation:
        conv = self._convs.get(conv_id)
        if conv is None:
            raise KeyError(
                f"未找到会话 `{conv_id}`；现有会话：{', '.join(self._convs) or '(无)'}"
            )
        return conv

    def has(self, conv_id: str) -> bool:
        return conv_id in self._convs

    def list_conversations(self, viewer: str = USER) -> list[dict[str, Any]]:
        """会话列表 —— 微信首页那种，带最后一条消息和未读红点。

        只返回 ``viewer`` 看得见的会话；未读也是**按人算**的。
        """
        items: list[dict[str, Any]] = []
        for cid in reversed(self._order):
            conv = self._convs.get(cid)
            if conv is None or not self.can_view(conv, viewer):
                continue
            items.append(self.summary(conv, viewer))
        return items

    def summary(self, conv: Conversation, viewer: str = USER) -> dict[str, Any]:
        """会话摘要（对外公开，供 HTTP / CLI 直接复用）。

        ``unread`` 按 ``viewer`` 算——旧实现写死 ``USER``，
        多主体下每个人看到的红点都是同一个，等于没有。
        """
        last = conv.last_message()
        return {
            "id": conv.id,
            "kind": conv.kind,
            "title": conv.title,
            "owner": conv.owner,
            "members": [
                {"id": m, "name": self._display_name(m)} for m in conv.members
            ],
            "contextId": conv.contextId,
            "messageCount": len(conv.messages),
            "unread": self.unread_count(conv, viewer),
            "frozen": conv.frozen,
            "lastMessage": last.model_dump(mode="json") if last else None,
            "updatedAt": conv.updatedAt,
        }

    def history(
        self, conv_id: str, limit: int = 200, viewer: str = USER
    ) -> dict[str, Any]:
        """拉取聊天记录（含每条消息的投递回执）。"""
        conv = self.get(conv_id)
        msgs = conv.messages[-limit:]
        ids = {m.id for m in msgs}
        return {
            "conversation": self.summary(conv, viewer),
            "messages": [m.model_dump(mode="json") for m in msgs],
            "deliveries": [
                d.model_dump(mode="json") for d in conv.deliveries if d.messageId in ids
            ],
        }

    # ------------------------------------------------------------------ #
    # 内部：注册 / 事件
    # ------------------------------------------------------------------ #

    def _register(self, conv: Conversation) -> None:
        self._convs[conv.id] = conv
        self._order.append(conv.id)
        while len(self._order) > 500:
            old = self._order.pop(0)
            self._convs.pop(old, None)
        log.info("会话已创建 %s (%s)：%s", conv.id, conv.kind, ", ".join(conv.members))

    def _touch(self, conv: Conversation) -> None:
        conv.updatedAt = utc_now()

    def _channel(self, conv_id: str) -> str:
        return f"im:{conv_id}"

    def _publish(self, conv: Conversation, event: str, **payload: Any) -> None:
        self._touch(conv)
        self.bus.publish(
            self._channel(conv.id),
            {
                "kind": "im-event",
                "event": event,
                "conversationId": conv.id,
                "ts": conv.updatedAt,
                **payload,
            },
        )

    def _system(self, conv: Conversation, text: str) -> ChatMessage:
        """追加一条系统消息（入群提示、失败原因等）。"""
        msg = ChatMessage(
            conversationId=conv.id,
            sender=SYSTEM,
            senderName="系统",
            text=text,
            kind="system",
        )
        conv.messages.append(msg)
        self._publish(conv, "message", message=msg.model_dump(mode="json"))
        return msg

    # ------------------------------------------------------------------ #
    # 未读 / 已读
    # ------------------------------------------------------------------ #

    def unread_count(self, conv: Conversation, reader: str) -> int:
        """某人未读的、来自他人的文本消息条数。"""
        visible = [m for m in conv.messages if m.sender != reader and m.kind == "text"]
        last_read = conv.reads.get(reader)
        if last_read is None:
            return len(visible)
        seen = False
        count = 0
        for m in conv.messages:
            if m.id == last_read:
                seen = True
                continue
            if seen and m.sender != reader and m.kind == "text":
                count += 1
        return count if seen else len(visible)

    def mark_read(self, conv_id: str, reader: str = USER) -> Conversation:
        """把会话标记为已读（前端打开会话时调用）。"""
        conv = self.get(conv_id)
        last = conv.last_message()
        if last is not None:
            conv.reads[reader] = last.id
        self._publish(conv, "read", reader=reader)
        return conv

    # ------------------------------------------------------------------ #
    # @ 提及
    # ------------------------------------------------------------------ #

    def parse_mentions(self, text: str) -> tuple[list[str], bool]:
        """从文本里解析 @ 提及。

        返回 ``(agent_id 列表, 是否 @所有人)``。
        认不出来的 @ 会被静默忽略（可能是邮箱、装饰性文字）。
        """
        found: list[str] = []
        broadcast = False
        for token in _MENTION_RE.findall(text):
            if token.lower() in _BROADCAST_TOKENS:
                broadcast = True
                continue
            rid = self._resolve_mention(token)
            if rid and rid not in found:
                found.append(rid)
        return found, broadcast

    def _resolve_mention(self, token: str) -> Optional[str]:
        """把 ``@token`` 解析成 agent id。

        先精确匹配 id / name；再退一步做 name 前缀匹配——因为中文名字常带空格
        （「回显 B」），而 ``@`` 后的正则只能抓到连续的中文或连续的字母，
        不补这一步就永远 @ 不上这类 agent。前缀匹配仅在**唯一命中**时生效，
        有歧义时宁可当没 @ 过。
        """
        low = token.lower()
        records = self.registry.list_records()
        for rec in records:
            if rec.id.lower() == low or rec.name.lower() == low:
                return rec.id
        hits = [r.id for r in records if r.name.lower().startswith(low)]
        return hits[0] if len(hits) == 1 else None

    def _resolve_targets(
        self, conv: Conversation, text: str, mentions: list[str], broadcast: bool
    ) -> list[str]:
        """决定这条消息唤醒谁。"""
        members = conv.member_ids()

        if broadcast:
            return list(members)

        if mentions:
            # 被 @ 的人必须在群里才算数
            return [m for m in mentions if m in members]

        if conv.kind == "direct":
            return list(members)

        # 群聊且没人被 @ —— 按能力自动挑一个"接话"的人
        if not conv.metadata.get("autoRoute", True):
            return []
        return self._auto_pick(conv, text)

    def _is_reachable(self, agent_id: str) -> bool:
        """只排除**明确**不可用的 agent；``unknown`` 仍给一次机会。

        少了这一步会踩坑：当群里没有一个 healthy 成员时，
        ``healthy or candidates`` 会退回全量候选，于是消息被丢给一个
        已经确定跑不起来的 agent。
        """
        return self.registry.get(agent_id).health.get("status") != "unavailable"

    def _auto_pick(self, conv: Conversation, text: str) -> list[str]:
        """群聊无人被 @ 时，挑一个最合适的成员接话。

        先剔除明确不可用的，再优先在健康成员里挑；一个健康的都没有时
        才退而求其次——总比全员沉默好，而且失败会明确呈现出来。
        """
        candidates = [
            m for m in conv.members if self.registry.has(m) and self._is_reachable(m)
        ]
        if not candidates:
            return []

        def best_of(pool: list[str]) -> Optional[str]:
            best: Optional[str] = None
            best_score = float("-inf")
            for mid in pool:
                score = self.registry.score(self.registry.get(mid), text)
                if score > best_score:
                    best, best_score = mid, score
            return best

        healthy = [
            m
            for m in candidates
            if self.registry.get(m).health.get("status") == "healthy"
        ]
        picked = best_of(healthy or candidates)
        return [picked] if picked else []

    # ------------------------------------------------------------------ #
    # 发消息（核心）
    # ------------------------------------------------------------------ #

    async def send(
        self,
        conv_id: str,
        text: str,
        sender: str = USER,
        reply_to: Optional[str] = None,
        wake: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """发一条消息。

        **立刻返回**：消息先落库并广播，被唤醒的 agent 在后台跑完再回话。
        这就是「聊天」——不会像 RPC 那样把调用方卡住。

        ``wake`` 可显式指定唤醒谁（覆盖 @ 解析），传空列表表示只存档不打扰。

        被门禁挡下的目标**不会静默消失**：会补一条系统消息说明原因。
        静默丢消息在协作场景里最伤人——你以为发了，对方根本没收到。
        """
        conv = self.get(conv_id)
        if not text.strip():
            raise ValueError("消息内容不能为空")
        if conv.frozen:
            raise ValueError("该会话已归档（好友关系已解除），不能继续发言")

        mentions, broadcast = self.parse_mentions(text)
        targets = (
            [w for w in wake if w in conv.members]
            if wake is not None
            else self._resolve_targets(conv, text, mentions, broadcast)
        )

        # 门禁。群聊走 ``via="group:<id>"``——那条通道只放宽 ``chat``，
        # 不继承 ``delegate`` 之类的执行类权限（否则建个群就能绕过好友制）。
        via = f"group:{conv.id}" if conv.kind == "group" else "direct"
        allowed: list[str] = []
        refused: list[str] = []
        for target in targets:
            if self.guard.can(sender, target, Scope.CHAT, via=via):
                allowed.append(target)
            else:
                refused.append(target)
        targets = allowed

        # 消息真的投出去了 —— 这才是「成功互动」的事实依据。
        # 信任衰减的 lastInteractAt 就靠这里刷新（§7.4）。
        for target in targets:
            self._note_interaction(sender, target)

        msg = ChatMessage(
            conversationId=conv.id,
            sender=sender,
            senderName=self._display_name(sender),
            text=text,
            mentions=mentions,
            replyTo=reply_to,
        )
        conv.messages.append(msg)

        deliveries: list[Delivery] = []
        for agent_id in targets:
            d = Delivery(
                messageId=msg.id,
                agentId=agent_id,
                agentName=self._display_name(agent_id),
            )
            conv.deliveries.append(d)
            deliveries.append(d)

        # 发送方自己显然读过自己刚发的内容
        conv.reads[sender] = msg.id

        self._publish(
            conv,
            "message",
            message=msg.model_dump(mode="json"),
            deliveries=[d.model_dump(mode="json") for d in deliveries],
        )

        if refused:
            self._system(conv, self._refusal_note(sender, refused, via))

        for d in deliveries:
            self._spawn(self._deliver(conv, msg, d))

        return {
            "message": msg.model_dump(mode="json"),
            "deliveries": [d.model_dump(mode="json") for d in deliveries],
            "woke": targets,
            "refused": refused,
        }

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    # ------------------------------------------------------------------ #
    # 内部：把一条消息投递给一位 agent
    # ------------------------------------------------------------------ #

    def _render_prompt(self, conv: Conversation, msg: ChatMessage, agent_id: str) -> str:
        """构造真正送给 agent 的 prompt：会话历史 + 本次发言。

        这一步是「记得上文」的关键。但历史不能无节制地灌——既会撑爆
        上下文窗口，也容易让 agent 误以为要回应全部历史。所以：
        只带最近 N 条，且用明确措辞标注它只是背景。
        """
        limit = int(conv.metadata.get("history", DEFAULT_HISTORY) or 0)
        current = msg
        prior = [m for m in conv.messages if m.id != current.id][-limit:]
        prior = [m for m in prior if m.kind == "text"]
        if not prior:
            return current.text

        lines: list[str] = []
        for m in prior:
            who = "你" if m.sender == agent_id else m.senderName or self._display_name(m.sender)
            lines.append(f"{who}：{m.text}")

        if len(conv.members) > 1:
            scene = (
                f"你正在一个名为「{conv.title}」的群聊里，"
                f"成员有：{'、'.join(self._display_name(m) for m in conv.members)}。"
            )
        else:
            # 写死「用户」在多主体下等于没说——agent 分不清是谁在跟它说话
            scene = f"你正在与「{self._display_name(current.sender)}」一对一对话。"

        return (
            f"{scene}\n"
            f"以下是本次对话此前的消息（仅作背景，不要逐条复述）：\n"
            f"{chr(10).join(lines)}\n\n"
            f"现在，请回应这条最新消息：\n{current.text}"
        )

    async def _deliver(self, conv: Conversation, msg: ChatMessage, d: Delivery) -> None:
        """把消息投递给单个 agent，并把它的产出作为新消息追加回会话。

        状态机：pending → delivered → read → replied / failed
        """
        if not self.registry.has(d.agentId):
            self._fail(conv, d, f"agent `{d.agentId}` 已不在注册表中")
            return

        rec: AgentRecord = self.registry.get(d.agentId)

        # 明确不可用的直接快速失败——省得白白等一个超时
        if rec.health.get("status") == "unavailable":
            detail = rec.health.get("detail") or "未配置或不可用"
            self._fail(conv, d, f"{rec.name} 当前不可用：{detail}")
            return

        d.state = DeliveryState.DELIVERED
        d.updatedAt = utc_now()
        self._publish(conv, "delivery", delivery=d.model_dump(mode="json"))

        prompt = self._render_prompt(conv, msg, d.agentId)
        message = Message.user(prompt, contextId=conv.contextId)
        task = self.registry.new_task(
            d.agentId,
            message,
            context_id=conv.contextId,  # ← 会话上下文复用，agent 才记得上文
            metadata={
                "im": True,
                "conversationId": conv.id,
                "messageId": msg.id,
                "sender": msg.sender,
            },
        )
        d.taskId = task.id
        msg.taskId = task.id
        conv.reads[d.agentId] = msg.id  # 对方已经开始读了

        try:
            marked = False
            async for event in self.registry.execute(rec, task, message):
                if not marked and task.status.state == TaskState.WORKING:
                    marked = True
                    d.state = DeliveryState.READ
                    d.updatedAt = utc_now()
                    self._publish(conv, "delivery", delivery=d.model_dump(mode="json"))
                _ = event

            if task.status.state == TaskState.COMPLETED:
                text = task.final_text().strip()
                if not text and task.status.message is not None:
                    text = task.status.message.text().strip()
                if not text:
                    text = "(对方没有产出内容)"
                reply = ChatMessage(
                    conversationId=conv.id,
                    sender=d.agentId,
                    senderName=rec.name,
                    text=text,
                    replyTo=msg.id,
                    taskId=task.id,
                )
                conv.messages.append(reply)
                d.state = DeliveryState.REPLIED
                d.updatedAt = utc_now()
                self._publish(
                    conv,
                    "message",
                    message=reply.model_dump(mode="json"),
                    delivery=d.model_dump(mode="json"),
                )
            else:
                reason = (
                    task.status.message.text()
                    if task.status.message
                    else task.status.state.value
                )
                self._fail(conv, d, reason)
        except asyncio.CancelledError:
            self._fail(conv, d, "投递被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("会话 %s 投递到 %s 失败", conv.id, d.agentId)
            self._fail(conv, d, f"{type(exc).__name__}: {exc}")

    def _fail(self, conv: Conversation, d: Delivery, reason: str) -> None:
        """失败必须让用户看得见——静默失败是协作场景里最糟的体验。"""
        d.state = DeliveryState.FAILED
        d.error = reason
        d.updatedAt = utc_now()
        self._publish(conv, "delivery", delivery=d.model_dump(mode="json"))
        self._system(conv, f"❌ {d.agentName} 未能回复：{reason}")

    # ------------------------------------------------------------------ #
    # 事件流（SSE）
    # ------------------------------------------------------------------ #

    async def stream(
        self, conv_id: str, timeout: float = 25.0
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅会话事件。

        遵循项目约定：**先订阅再发快照**，否则快 agent 的事件会掉进
        订阅窗口里被丢掉。
        """
        conv = self.get(conv_id)
        q = self.bus.subscribe(self._channel(conv_id))
        try:
            yield {
                "kind": "im-event",
                "event": "snapshot",
                "conversationId": conv_id,
                **self.history(conv_id, limit=50),
            }
            while True:
                got, item = await self.bus.next_event(q, timeout)
                if not got:
                    yield {"kind": "im-event", "event": "heartbeat", "conversationId": conv_id}
                    continue
                yield item
        finally:
            self.bus.unsubscribe(self._channel(conv_id), q)

    async def wait_idle(self, conv_id: str, timeout: float = 120.0) -> None:
        """等待某个会话的所有在途投递结束（CLI / 测试用）。

        聊天本身是异步的，但命令行里需要"等到对方都回完了"再退出。
        """
        deadline = time.monotonic() + timeout
        while True:
            conv = self.get(conv_id)
            if all(d.is_terminal for d in conv.deliveries):
                return
            if time.monotonic() > deadline:
                log.warning("会话 %s 等待超时（%.0fs），仍有在途投递", conv_id, timeout)
                return
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)


__all__ = [
    "Conversation",
    "ChatMessage",
    "Delivery",
    "DeliveryState",
    "SocialHub",
    "USER",
    "SYSTEM",
]
