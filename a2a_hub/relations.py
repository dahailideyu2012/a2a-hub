"""社交图谱（关系层）—— 把 agent 当独立个体，用双向关系图做访问控制。

为什么是「关系图」而不是「授权表」
----------------------------------
旧设计把 agent 当资源，只需要一张单向白名单 ``主体 → 可用 agent``。
新设计把 agent 当邻居：关系是**双向边**（申请 + 同意才成立），
而权限是**单向授予**（A 给 B 的权限与 B 给 A 的权限相互独立）。
两者分开，才能表达「我们是好友，但你不能使唤我」。

三个概念
--------
    Member        节点：人和 agent 同表，差别只在 kind 与 owner
    Relationship  边：pair 规范化成字典序，一份记录对双方都成立
    Grant         边的方向性载荷：granter 授予 grantee 的 scope 集合

注意 ``grants`` 的 key 是**被授予方**：``grants["b"]`` 表示「b 被允许做什么」，
即别人给 b 的权限。写反会变成「谁能管我」，语义清楚但极易搞错，
所以一律通过 :meth:`SocialGraph.grant_from` / :meth:`can` 访问，不裸用字典。

两条必须守住的不变量
--------------------
1. **能聊天 ≠ 能指挥你干活。**
   同意好友只默认给 ``peek/chat/invite``；``delegate``（派任务执行）
   一律要显式授予。这是整套权限设计里最重要的一条默认值。
2. **权限上行闭包。** ``grant(A→B) ⊆ owned_scopes(A)``。
   否则 B 只要跟 A 交上朋友，就能绕道拿到 A 的 owner 的资源——
   社交网络里最典型的提权路径（transitive privilege escalation）。

群聊是权限放大器，所以有一条铁律
--------------------------------
**同群只放宽 ``chat``，绝不放宽 ``delegate``。**
少了它，任何主体建个群就能绕过好友制去驱动别人的 agent。

退化行为
--------
没有 ``members.yaml`` 时 ``enabled=False``，:meth:`SocialGraph.can` 恒真、
:meth:`visible_contacts` 返回 ``None``（表示不过滤）——
行为与 v0.3.0 逐字节一致，既有测试不受影响。
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field

from .autonomy import (
    EXECUTION_SCOPES,
    STALE_DAYS,
    AutonomyPolicy,
    Decision,
    decide_accept,
    decide_approve,
    decide_send,
    sanitize_reason,
)
from .models import utc_now

log = logging.getLogger("a2a_hub.relations")

#: 成员 id 的已知前缀。带前缀 → 成员；否则按 agent 处理（补 ``agent:``）。
MEMBER_PREFIXES: tuple[str, ...] = ("human:", "agent:", "bot:", "svc:")

#: 会话层的历史保留字，永远不作为成员 id 出现。
USER = "user"
SYSTEM = "system"

#: 无 members.yaml 时退化出的默认成员。
DEFAULT_MEMBER = "human:default"

#: 申请被拒后的冷却时长（秒）。太短等于鼓励骚扰，太长会挡住正常的「改主意」。
REJECT_COOLDOWN = 24 * 3600

#: 审计日志的保留条数上限（内存 + 落盘都按这个截断）。
AUDIT_LIMIT = 2000

#: 自主交友待办队列的上限（远超这个数说明策略配得太松了）。
APPROVAL_LIMIT = 500

#: 被拒后多久之内不再推荐同一个人 —— 骚扰的成本要落在评分上，不能只靠人自觉。
REJECT_PENALTY_DAYS = 30

#: 匹配打分的权重（§6.2）。三项相加为 1.0，被拒惩罚单独减。
AFFINITY_WEIGHTS = {"skill": 0.45, "fof": 0.30, "kind": 0.15}
AFFINITY_REJECT_PENALTY = 0.10

#: 共同好友数归一化的基准：3 个共同好友就算「很熟」。
FOF_SATURATION = 3

#: 社交距离里「不可达」的哨兵值。
DISTANCE_UNREACHABLE = 99

_LATIN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """把一段文本切成可比对的 token（拉丁词 + 中文 bigram）。

    刻意不复用 ``registry._tokenize``：关系层**不许**把执行层拖进来
    （见类 docstring），而且两边要的东西本来就不同——这里只需要
    「两段能力描述像不像」，不需要路由那套加权。
    """
    low = (text or "").lower()
    out: set[str] = set(_LATIN_RE.findall(low))
    cjk = re.findall(r"[\u4e00-\u9fff]+", low)
    for run in cjk:
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i : i + 2] for i in range(len(run) - 1))
    return out


class Scope(str, Enum):
    """交流权限的档位。前三个是对话类（默认给），后四个是执行类（必须显式给）。"""

    PEEK = "peek"
    CHAT = "chat"
    INVITE = "invite"
    PROFILE = "profile"
    DELEGATE = "delegate"
    ARTIFACT = "artifact"
    ADMIN = "admin"


#: 规范顺序 —— 输出与比较都用它，避免集合顺序导致的噪音 diff。
SCOPE_ORDER: tuple[Scope, ...] = (
    Scope.PEEK,
    Scope.CHAT,
    Scope.INVITE,
    Scope.PROFILE,
    Scope.DELEGATE,
    Scope.ARTIFACT,
    Scope.ADMIN,
)

#: 同意好友即默认授予的对话类 scope。
DEFAULT_FRIEND_SCOPES: tuple[Scope, ...] = (Scope.PEEK, Scope.CHAT, Scope.INVITE)

#: 必须显式授予的执行类 scope —— 默认一律不给。
PRIVILEGED_SCOPES: tuple[Scope, ...] = (
    Scope.PROFILE,
    Scope.DELEGATE,
    Scope.ARTIFACT,
    Scope.ADMIN,
)

#: **群聊通道里允许的 scope。** 只放宽 chat，绝不继承执行类。
GROUP_SCOPES: tuple[Scope, ...] = (Scope.PEEK, Scope.CHAT)


def _cap_text(caps: dict[str, Any]) -> str:
    """把能力画像压成一段可比较的文本。"""
    parts = [
        str(caps.get("name") or ""),
        str(caps.get("bio") or ""),
        str(caps.get("description") or ""),
    ]
    parts.extend(str(t) for t in (caps.get("tags") or []))
    parts.extend(str(s) for s in (caps.get("skills") or []))
    return " ".join(parts)


def _sorted_scopes(values: Iterable[Any]) -> list[Scope]:
    """去重 + 按 :data:`SCOPE_ORDER` 排序。"""
    out: list[Scope] = []
    for v in values or []:
        s = v if isinstance(v, Scope) else Scope(v)
        if s not in out:
            out.append(s)
    return sorted(out, key=SCOPE_ORDER.index)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class SocialError(Exception):
    """社交层错误基类。"""

    status = 400


class SocialNotPermitted(SocialError):
    """无权做这件事（HTTP 403）。"""

    status = 403


class SocialConflict(SocialError):
    """状态冲突，比如重复申请、已是好友（HTTP 409）。"""

    status = 409


class SocialNotFound(SocialError):
    """成员或关系不存在（HTTP 404）。"""

    status = 404


class RefusalReason(str, Enum):
    """拒绝的原因码。

    门禁拒绝**必须能指路**：``403`` / ``-32008`` 只告诉调用方「不行」，
    而调用方真正需要知道的是「我差哪一步」。这些码就是那句话的机器可读形式，
    会原样进 JSON-RPC 的 ``data.reason`` 与 HTTP 的 ``detail``。
    """

    NOT_FRIEND = "not_friend"          # 还不是好友
    MISSING_SCOPE = "missing_scope"    # 是好友，但这一档没被授予
    GROUP_BOUNDARY = "group_boundary"  # 群聊通道只放宽 chat，执行类一律不放
    BLOCKED = "blocked"                # 被拉黑（对外措辞要中性，见 §13）
    SOCIAL_DISABLED = "social_disabled"  # 压根没启用社交层
    STALE = "stale"                    # 长期无互动，执行类权限已降级（§7.4）


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


class RelationState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    FRIEND = "friend"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class Member(BaseModel):
    """网络里的一个节点。人和 agent 同表。

    ``owner`` 是这一版最重要的字段：agent 的归属人。
    自主交友必须有人兜底——agent 自己同意了一个不该同意的陌生人，
    责任要能落到具体的人头上。``owner`` 为空 = 禁止自主交友。

    ``max_scopes`` 是这个人/agent 能授予出去的天花板，
    用于实现权限上行闭包；``None`` 表示不额外限制。
    """

    id: str
    name: str = ""
    kind: Literal["human", "agent", "service"] = "human"
    owner: Optional[str] = None
    bio: str = ""
    discoverable: Literal["private", "circle", "public"] = "private"
    tokens: list[str] = Field(default_factory=list)
    default_agent: Optional[str] = None
    max_scopes: Optional[list[Scope]] = None
    autonomy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    def display_name(self) -> str:
        return self.name or self.id


class Grant(BaseModel):
    """``granter`` 授予 ``grantee`` 的权限。单向。

    存储位置是 ``Relationship.grants[grantee]``，见模块 docstring 的说明。
    """

    scopes: list[Scope] = Field(default_factory=list)
    grantedBy: str = ""
    expiresAt: Optional[str] = None
    note: str = ""

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes


class Relationship(BaseModel):
    """两个成员之间的一条边。**一份记录对双方都成立**。

    ``pair`` 必须规范化成字典序（``a <= b``），否则 ``(A,B)`` 与 ``(B,A)``
    会变成两条互相打架的记录——这是图结构最经典的 bug。
    """

    pair: tuple[str, str]
    state: RelationState = RelationState.NONE
    requestedBy: str = ""
    requestMessage: str = ""
    requestedScopes: list[Scope] = Field(default_factory=list)
    grants: dict[str, Grant] = Field(default_factory=dict)
    blocked: dict[str, bool] = Field(default_factory=dict)
    #: 引荐记录：``{by, for, note, ts}``。``by`` 是引荐人，``for`` 是被引荐给谁。
    #: 引荐**不授予任何 scope**——它只提高可信度，并让 private 成员可被发现。
    referrals: list[dict[str, Any]] = Field(default_factory=list)
    createdAt: str = ""
    updatedAt: str = ""
    lastInteractAt: str = ""

    def has(self, member_id: str) -> bool:
        return member_id in self.pair

    def other(self, member_id: str) -> str:
        return self.pair[1] if member_id == self.pair[0] else self.pair[0]


# --------------------------------------------------------------------------- #
# 门禁协议 —— 会话层只调用它，不实现策略
# --------------------------------------------------------------------------- #


@runtime_checkable
class FriendshipGuard(Protocol):
    """会话层眼里的门禁。

    刻意只有三个方法：判定「能不能」、通讯录里「有哪些」、以及「叫什么」。
    策略全在 :class:`SocialGraph` 里，``social.py`` 不做任何权限判断——
    这样会话层在单测里注入 ``NullGuard`` 就是零依赖的。
    """

    enabled: bool

    def can(
        self, actor: str, peer: str, scope: Scope | str, *, via: str = "direct"
    ) -> bool: ...

    def visible_contacts(self, viewer: str) -> Optional[set[str]]: ...

    def display_name(self, ref: str) -> str: ...


class NullGuard:
    """无配置时的退化实现：全放行。

    ``visible_contacts`` 返回 ``None`` 表示「不过滤」，
    而不是「空集合」——两者的语义完全不同，别搞混。
    """

    enabled = False

    def can(
        self, actor: str, peer: str, scope: Scope | str, *, via: str = "direct"
    ) -> bool:
        return True

    def visible_contacts(self, viewer: str) -> Optional[set[str]]:
        return None

    def display_name(self, ref: str) -> str:
        return ""


# --------------------------------------------------------------------------- #
# 社交图谱
# --------------------------------------------------------------------------- #


class SocialGraph:
    """关系图 + 判定 + 持久化。

    与 registry 的关系：**registry 管 agent 的「存在性」，本类管「关系」**。
    本类不 import registry，以免把执行层拖进社交层；
    调用方用 :meth:`ensure_agents` 把 registry 里的 agent 补成图的节点。
    """

    def __init__(
        self,
        members: Optional[list[Member]] = None,
        *,
        mode: str = "strict",
        path: Optional[Path | str] = None,
        owner_full_access: bool = True,
    ) -> None:
        self._members: dict[str, Member] = {m.id: m for m in (members or [])}
        self.mode = (mode or "strict").lower()
        self.path = Path(path) if path else None
        self.owner_full_access = owner_full_access
        self._rels: dict[tuple[str, str], Relationship] = {}
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        #: 能力解析器。**刻意不 import registry**——由调用方注入一个
        #: ``fn(member_id) -> {"skills": [...], "tags": [...], "bio": "..."}``。
        #: 没注入时退化为只用 ``Member.bio`` 打分（够用，且零依赖）。
        self._capabilities: Optional[Any] = None
        #: 待 owner 审批的自主交友申请（阶段 4 用）。
        self._approvals: list[dict[str, Any]] = []
        #: 自主交友的配额计数：``{member_id: {"date": "YYYY-MM-DD", "sent": n}}``。
        self._quotas: dict[str, dict[str, Any]] = {}
        #: 有成员且模式不是 off，门禁才真正生效。
        self.enabled = bool(self._members) and self.mode != "off"
        self.load()

    # ------------------------------------------------------------------ #
    # 成员与规范化
    # ------------------------------------------------------------------ #

    @property
    def default_member_id(self) -> str:
        """``USER`` 这个保留字映射到谁。

        优先用显式声明的 ``human:default``；只声明了一个人类时就用他——
        这样单人自用场景下旧的 ``sender="user"`` 路径不用改。
        """
        if DEFAULT_MEMBER in self._members:
            return DEFAULT_MEMBER
        humans = [m.id for m in self._members.values() if m.kind == "human"]
        return humans[0] if len(humans) == 1 else DEFAULT_MEMBER

    def normalize(self, ref: str) -> str:
        """把裸 agent id / 保留字 归一化成成员 id。

        带已知前缀 → 原样；``user``/``system`` → 默认人类；其余 → ``agent:<ref>``。
        """
        if not ref:
            return self.default_member_id
        if ref in (USER, SYSTEM):
            return self.default_member_id if ref == USER else SYSTEM
        head = ref.split(":", 1)[0] + ":"
        if head in MEMBER_PREFIXES:
            return ref
        return f"agent:{ref}"

    @staticmethod
    def agent_id_of(member_id: str) -> Optional[str]:
        """``agent:codex`` → ``codex``；不是 agent 成员则返回 ``None``。"""
        if member_id.startswith("agent:"):
            return member_id[len("agent:") :]
        return None

    def get_member(self, ref: str) -> Optional[Member]:
        return self._members.get(self.normalize(ref))

    def member_or_synthetic(self, ref: str) -> Member:
        """拿成员；没声明就合成一个只含 id 的（关系图不要求节点先声明）。"""
        mid = self.normalize(ref)
        m = self._members.get(mid)
        if m is not None:
            return m
        if mid == DEFAULT_MEMBER:
            # 退化模式下的「我」——显示名与 v0.3.0 的 ``USER`` 保持一致
            return Member(id=mid, name="我", kind="human")
        kind: Literal["human", "agent", "service"] = (
            "agent" if mid.startswith("agent:") else "human"
        )
        return Member(id=mid, kind=kind)

    def name_of(self, ref: str) -> str:
        m = self.get_member(ref)
        return m.name if m and m.name else ""

    def display_name(self, ref: str) -> str:
        """给会话层用的显示名。认不出就返回空串，由调用方回落。"""
        if ref in (USER, SYSTEM):
            return ""
        return self.member_or_synthetic(ref).display_name()

    def all_members(self) -> list[Member]:
        return list(self._members.values())

    def agent_member_ids(self) -> list[str]:
        return [m.id for m in self._members.values() if m.kind == "agent"]

    def ensure_agents(self, items: Iterable[tuple[str, str]]) -> None:
        """把 registry 里的 agent 补成图节点（缺声明时合成，``owner`` 留空）。

        合成的成员 ``discoverable`` 默认是 ``private``——**私有只挡「被发现」，
        不挡「被指名申请」**，所以别人知道你 id 时依然可以发申请，
        这是有意为之（否则新 agent 永远无法被加）。
        """
        for agent_id, name in items:
            mid = f"agent:{agent_id}"
            if mid not in self._members:
                self._members[mid] = Member(id=mid, name=name or agent_id, kind="agent")

    def resolve_token(self, token: str) -> Optional[Member]:
        """按 Bearer token 找成员。定长比较，防时序侧信道。"""
        if not token:
            return None
        for m in self._members.values():
            for t in m.tokens:
                if t and hmac.compare_digest(t, token):
                    return m
        return None

    def any_tokens(self) -> bool:
        """是否有任何成员配了**非空** token。

        没有的话说明这只是「声明了成员但还没配身份」的过渡状态，
        应当按本地开发模式放行——否则加一个空 members.yaml 就会把所有人锁在门外。

        **必须跳过空串**：`tokens: ["${A2A_TOKEN_X}"]` 在环境变量没设时会展开成
        `[""]`，列表非空但实际没有可用凭据。若按「列表非空」判断，就会一边
        认不出任何 token（全部 403），一边拒绝匿名请求（401），把人锁死在门外。
        """
        return any(t for m in self._members.values() for t in m.tokens)

    # ------------------------------------------------------------------ #
    # 边的存储
    # ------------------------------------------------------------------ #

    def _key(self, a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def relation(self, a: str, b: str) -> Relationship:
        """取关系（不落库）。不存在则返回一个 state=none 的空记录。"""
        ka, kb = self.normalize(a), self.normalize(b)
        rel = self._rels.get(self._key(ka, kb))
        return rel if rel is not None else Relationship(pair=self._key(ka, kb))

    def _ensure(self, a: str, b: str) -> Relationship:
        ka, kb = self.normalize(a), self.normalize(b)
        key = self._key(ka, kb)
        rel = self._rels.get(key)
        if rel is None:
            rel = Relationship(pair=key, createdAt=utc_now())
            self._rels[key] = rel
        return rel

    def is_friend(self, a: str, b: str) -> bool:
        rel = self._rels.get(self._key(self.normalize(a), self.normalize(b)))
        return rel is not None and rel.state is RelationState.FRIEND

    def is_blocked(self, a: str, b: str) -> bool:
        """单向拉黑，但**任一方向成立即视为断开**。"""
        rel = self._rels.get(self._key(self.normalize(a), self.normalize(b)))
        if rel is None:
            return False
        na, nb = self.normalize(a), self.normalize(b)
        return bool(rel.blocked.get(na) or rel.blocked.get(nb))

    def grant_from(self, granter: str, grantee: str) -> Grant:
        """``granter`` 给了 ``grantee`` 什么。不存在则返回空 Grant。"""
        rel = self._rels.get(self._key(self.normalize(granter), self.normalize(grantee)))
        if rel is None:
            return Grant()
        return rel.grants.get(self.normalize(grantee), Grant())

    def friends_of(self, ref: str) -> list[str]:
        mid = self.normalize(ref)
        out = [
            r.other(mid)
            for r in self._rels.values()
            if r.state is RelationState.FRIEND and r.has(mid)
        ]
        return sorted(out)

    # ------------------------------------------------------------------ #
    # 圈层：距离、共同好友、可见性
    # ------------------------------------------------------------------ #

    def mutual_friends(self, a: str, b: str) -> list[str]:
        """a 与 b 的共同好友**名单**。

        **只在内部用。** 对外一律只出数量（:meth:`common_friends`）——
        好友名单一旦能被非好友读到，加一个人就等于交出整个通讯录，
        再扩散一轮就拿到了全图（§7.2 的隐私铁律）。
        """
        return sorted(set(self.friends_of(a)) & set(self.friends_of(b)))

    def common_friends(self, a: str, b: str) -> int:
        """共同好友**数量** —— 社交网络里最强的信任信号，也是唯一可外露的形式。"""
        return len(self.mutual_friends(a, b))

    def distance(self, a: str, b: str) -> int:
        """社交距离：``0`` 自己 · ``1`` 好友 · ``2`` 好友的好友 · ``99`` 不可达。

        ``d2`` 是**可见但不通**的——它能出现在发现列表里、能被申请，
        但说话仍然要对方点头。这是「扩大圈层」与「不被陌生人打扰」的平衡点。
        """
        x, y = self.normalize(a), self.normalize(b)
        if x == y:
            return 0
        if self.is_friend(x, y):
            return 1
        if self.common_friends(x, y) > 0:
            return 2
        return DISTANCE_UNREACHABLE

    def visible_to(self, viewer: str, ref: str) -> bool:
        """``viewer`` 能不能**发现**（搜到）``ref``。

        三档 ``discoverable`` + 距离规则：

        - 自己 / 被共同好友引荐过 → 总能发现
        - ``private``（默认）→ **谁都不能**，只能被已有好友引荐
        - ``circle`` → 度 ≤ 2（好友 + 好友的好友）
        - ``public`` → 任何人

        **owner 不是一条发现通道。** 「能不能发现」讲的是社交面（找**新**对象），
        而 owner 对自己的 agent 本来就是全权（见 :meth:`why_not` 里的 owner 短路），
        不需要靠被发现来建立关系——:meth:`discover` 也本就把自己的 agent 排除在外。
        把 owner 当成「可见」会把 ``private`` 成员在 owner 视角下漏出去，
        而 ``private`` 的语义恰恰是「**任何人**都发现不了，只能被引荐」。
        """
        a, b = self.normalize(viewer), self.normalize(ref)
        if a == b:
            return True
        m = self._members.get(b)
        if m is None:
            # 合成节点（外部成员 / 未声明的远端 agent）没有 discoverable 声明，
            # 按「可见」处理——否则会出现「看得见 ID 却永远搜不到自己」的怪事。
            return True
        if self._has_referral_for(a, b):
            return True
        if m.discoverable == "private":
            return False
        if m.discoverable == "public":
            return True
        return self.distance(a, b) <= 2

    def _has_referral_for(self, viewer: str, ref: str) -> bool:
        """``ref`` 是否被某位共同好友引荐给 ``viewer`` 过。"""
        rel = self._rels.get(self._key(self.normalize(viewer), self.normalize(ref)))
        if rel is None:
            return False
        return any(r.get("for") == self.normalize(viewer) for r in rel.referrals)

    # ------------------------------------------------------------------ #
    # 能力解析与匹配打分（§6.2）
    # ------------------------------------------------------------------ #

    def set_capability_resolver(self, fn: Optional[Any]) -> None:
        """注入能力解析器 ``fn(member_id) -> dict``。

        走注入而不是直接 import registry，是为了守住「关系层不拖进执行层」
        这条分层纪律；同时也让纯关系图的单测可以完全不碰 registry。
        """
        self._capabilities = fn

    def capabilities(self, ref: str) -> dict[str, Any]:
        """某成员的能力画像。声明里的 ``bio`` 是兜底，解析器可覆盖/补充。"""
        mid = self.normalize(ref)
        m = self._members.get(mid)
        base: dict[str, Any] = {
            "name": m.display_name() if m else mid,
            "bio": m.bio if m else "",
            "tags": [],
            "skills": [],
        }
        if self._capabilities is not None:
            try:
                extra = self._capabilities(mid) or {}
            except Exception:  # noqa: BLE001  解析器出问题不该让打分崩掉
                log.debug("能力解析器调用失败 %s", mid, exc_info=True)
                extra = {}
            for k, v in extra.items():
                if v:
                    base[k] = v
        return base

    def _recently_rejected(self, a: str, b: str) -> bool:
        """最近 30 天内这条边被拒过吗。

        骚扰的成本必须落在评分上——只靠「人不该乱加」的自觉是不行的。
        """
        rel = self._rels.get(self._key(self.normalize(a), self.normalize(b)))
        if rel is None or rel.state is not RelationState.REJECTED or not rel.updatedAt:
            return False
        try:
            when = datetime.fromisoformat(rel.updatedAt)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - when
        return age.days < REJECT_PENALTY_DAYS

    def affinity(self, mid: str, candidate: str, need: str = "") -> dict[str, Any]:
        """匹配打分：``0.45·技能互补 + 0.30·共同好友 + 0.15·同类偏好 − 0.10·被拒惩罚``。

        返回分数 + **各维度明细**。明细不是装饰：自主交友必须能事后回答
        「为什么选了它」，光存一个总分是解释不了的（§6 约束 3）。
        """
        me, other = self.normalize(mid), self.normalize(candidate)
        my_tokens = _tokenize(_cap_text(self.capabilities(me)))
        other_tokens = _tokenize(_cap_text(self.capabilities(other)))
        need_tokens = _tokenize(need)

        # 技能互补：need 命中多少；再用「我已有的能力」折掉重复的
        # （不折的话会加一堆同类重复的 agent，圈层只变胖不变强）
        skill_fit = (
            len(need_tokens & other_tokens) / len(need_tokens) if need_tokens else 0.5
        )
        blankness = (
            1.0 - len(my_tokens & other_tokens) / len(other_tokens)
            if other_tokens
            else 1.0
        )
        complement = skill_fit * blankness

        fof = min(1.0, self.common_friends(me, other) / FOF_SATURATION)
        kind_pref = (
            1.0
            if self.member_or_synthetic(me).kind == self.member_or_synthetic(other).kind
            else 0.5
        )
        penalty = 1.0 if self._recently_rejected(me, other) else 0.0

        score = (
            AFFINITY_WEIGHTS["skill"] * complement
            + AFFINITY_WEIGHTS["fof"] * fof
            + AFFINITY_WEIGHTS["kind"] * kind_pref
            - AFFINITY_REJECT_PENALTY * penalty
        )
        return {
            "affinity": round(max(0.0, score), 4),
            "breakdown": {
                "skill": round(complement, 4),
                "fof": round(fof, 4),
                "kind": round(kind_pref, 4),
                "rejectPenalty": round(penalty, 4),
            },
            "commonFriends": self.common_friends(me, other),
            "distance": self.distance(me, other),
        }

    def discover(self, mid: str, need: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """自主发现：给 ``mid`` 推荐值得认识的对象，按匹配度排序。

        候选必须同时满足「可见」和「还没有关系」：

        - 已经是好友 / 已有在途申请 / 被拉黑 → 不必推荐
        - 自己的 agent → 本来就全权，推荐没有意义
        - ``private`` 且没被引荐过 → 不出现（§7.1）

        每条结果都带 ``breakdown``，让「为什么推荐它」是可解释的。
        """
        me = self.normalize(mid)
        out: list[dict[str, Any]] = []
        for cand_m in self._members.values():
            cand = cand_m.id
            if cand == me or self._is_owner(me, cand):
                continue
            if self.is_blocked(me, cand) or self.is_friend(me, cand):
                continue
            rel = self._rels.get(self._key(me, cand))
            if rel is not None and rel.state is RelationState.PENDING:
                continue
            if not self.visible_to(me, cand):
                continue
            scored = self.affinity(me, cand, need)
            out.append(
                {
                    **self.card(cand_m),
                    **scored,
                    "referred": self._has_referral_for(me, cand),
                    "matchPercent": int(round(scored["affinity"] * 100)),
                }
            )
        out.sort(key=lambda d: d["affinity"], reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ #
    # 引荐（d2 → d1 的唯一自然通道）
    # ------------------------------------------------------------------ #

    def introduce(self, by: str, target: str, peer: str, note: str = "") -> dict[str, Any]:
        """``by`` 把 ``peer`` 引荐给 ``target``。

        资格有两段，缺一不可：

        - ``by`` 必须是 **target 的好友**（引荐要递到对方手里，得先说得上话）；
        - ``by`` 必须能**为 peer 背书**——是 peer 的好友，**或者是 peer 的 owner**。
          owner 对自己的 agent 天然全权，让它引荐自己的 agent 是理所当然；
          要求 owner 先跟自己的 agent「加好友」纯属荒唐。

        不设这道限制，引荐就成了绕过 ``discoverable`` 的任意后门。
        引荐**不授予任何 scope**：它只提高申请的优先级和可信度，
        并让 ``private`` 成员能被 target 发现（那是 private 唯一的对外通道）。
        """
        b, t, p = self.normalize(by), self.normalize(target), self.normalize(peer)
        if t == p:
            raise SocialError("不能把一个人引荐给他自己")
        if not self.is_friend(b, t):
            raise SocialNotPermitted("只有对方的好友才能引荐给他")
        if not (self.is_friend(b, p) or self._is_owner(b, p)):
            raise SocialNotPermitted("只有 peer 的好友或其 owner 才能为其背书")
        rel = self._ensure(t, p)
        entry = {"by": b, "for": t, "note": (note or "").strip(), "ts": utc_now()}
        # 同一个人重复引荐只留最新一条，免得名单里堆一串同款
        rel.referrals = [
            r for r in rel.referrals if not (r.get("by") == b and r.get("for") == t)
        ] + [entry]
        rel.updatedAt = utc_now()
        self._record("introduce", b, p, note=f"引荐给 {t}：{entry['note']}")
        self.save()
        return {**self.profile(p, t), "introducer": b, "note": entry["note"]}

    def introductions(self, ref: str) -> list[dict[str, Any]]:
        """别人引荐给 ``ref`` 的人（收件箱式）。"""
        mid = self.normalize(ref)
        out: list[dict[str, Any]] = []
        for rel in self._rels.values():
            for r in rel.referrals:
                if r.get("for") != mid:
                    continue
                peer = rel.other(mid) if rel.has(mid) else min(rel.pair)
                out.append(
                    {
                        "peer": peer,
                        "peerName": self.name_of(peer) or peer,
                        "introducer": r.get("by", ""),
                        "note": r.get("note", ""),
                        "ts": r.get("ts", ""),
                    }
                )
        out.sort(key=lambda d: d.get("ts") or "", reverse=True)
        return out

    def profile(self, ref: str, viewer: str) -> dict[str, Any]:
        """别人眼里的我。

        **只有共同好友「数」，没有好友名单。** 这是 §7.2 那条隐私铁律的落点：
        一旦名单可读，加一个人就等于交出整个通讯录。
        """
        mid, me = self.normalize(ref), self.normalize(viewer)
        return {
            "member": self.card(self.member_or_synthetic(mid)),
            "isSelf": me == mid,
            "isFriend": self.is_friend(me, mid),
            "commonFriends": self.common_friends(me, mid),
            "distance": self.distance(me, mid),
            "visible": self.visible_to(me, mid),
            "referred": self._has_referral_for(me, mid),
        }

    # ------------------------------------------------------------------ #
    # 权限上行闭包
    # ------------------------------------------------------------------ #

    def owned_scopes(self, ref: str, _depth: int = 0) -> set[Scope]:
        """某成员「自己拥有的」权限集合 —— 上行闭包的基准。

        规则：

        - 人类 / 服务账号：自己的一切由自己支配，天花板是 ``max_scopes``。
        - agent：天花板 = 自己的 ``max_scopes`` ∩ **owner 的**天花板。
        - 找不到 owner 的 agent 只拿得到对话类 —— 它没有人为执行类权限兜底。
        """
        mid = self.normalize(ref)
        m = self._members.get(mid)
        if m is None:
            return set(DEFAULT_FRIEND_SCOPES) if mid.startswith("agent:") else set(Scope)
        own = set(m.max_scopes) if m.max_scopes is not None else set(Scope)
        if m.kind != "agent":
            return own
        if _depth > 8:  # agent 套 agent 的病态配置，别把栈打穿
            return own & set(DEFAULT_FRIEND_SCOPES)
        owner = self.effective_owner(mid)
        if not owner:
            return own & set(DEFAULT_FRIEND_SCOPES)
        return own & self.owned_scopes(owner, _depth + 1)

    def effective_owner(self, ref: str) -> Optional[str]:
        """agent 的**实际**归属人。

        显式 ``owner`` 优先；没声明时回落到**唯一的人类成员**——
        单人自用是最常见的部署形态，要求把每个 agent 的 owner 都手写一遍
        纯属折磨，而且漏写一个就等于那个 agent 谁都使唤不动。

        多人类（多租户）场景下**不再回落**：没有 owner 的 agent 拿不到执行类
        权限，等于强制你显式声明归属。这是刻意的。
        """
        mid = self.normalize(ref)
        m = self._members.get(mid)
        if m is not None and m.owner:
            return self.normalize(m.owner)
        if m is not None and not m.is_agent:
            return None
        if m is None and not mid.startswith("agent:"):
            return None
        humans = [h.id for h in self._members.values() if h.kind == "human"]
        return humans[0] if len(humans) == 1 else None

    def _is_owner(self, actor: str, target: str) -> bool:
        """``actor`` 是不是 ``target`` 的归属人（target 必须是 agent）。"""
        if not self.owner_full_access:
            return False
        mid = self.normalize(target)
        m = self._members.get(mid)
        if m is not None and not m.is_agent:
            return False
        if m is None and not mid.startswith("agent:"):
            return False
        owner = self.effective_owner(mid)
        return bool(owner) and owner == self.normalize(actor)

    def _can_act_for(self, actor: str, target: str) -> bool:
        """``actor`` 能否代表 ``target`` 做社交决策。

        只有两种合法情形：自己代表自己，owner 代表自己的 agent。
        **不能代表别人**——否则「代同意」会成为绕过好友制的后门。
        """
        return actor == target or self._is_owner(actor, target)

    def can_act_for(self, actor: str, target: str) -> bool:
        """公开版的 :meth:`_can_act_for`，给「以某成员视角查看」的只读接口用。

        读接口（我的名片 / 我的关系 / 我的收件箱）允许 owner 带上
        ``as=<自己的 agent>`` 去看 agent 的视角——否则 agent 收到的申请
        就没人能处理了（agent 自己不会点「同意」）。写入侧本来就走
        :meth:`accept` / :meth:`reject` 的同名校验，两边共用这一条规则。
        """
        return self._can_act_for(self.normalize(actor), self.normalize(target))

    # ------------------------------------------------------------------ #
    # 核心判定
    # ------------------------------------------------------------------ #

    def why_not(
        self, actor: str, peer: str, scope: Scope | str, *, via: str = "direct"
    ) -> Optional[dict[str, Any]]:
        """:meth:`can` 的「为什么不行」版本。

        返回 ``None`` 表示放行；否则返回一个可直接塞进错误载荷的 dict
        （``reason`` / ``peer`` / ``needScope`` / 已授予的档位）。

        **``can()`` 就是它的布尔化**，两者共用同一段判定——分开写两份
        条件迟早会漂移，那正是权限系统最不该出现的 bug。
        """
        if not self.enabled:
            return None
        if actor == SYSTEM or peer == SYSTEM:
            return None

        a, b = self.normalize(actor), self.normalize(peer)
        if a == b:
            return None
        s = scope if isinstance(scope, Scope) else Scope(scope)

        # 拉黑优先级最高：任一方向拉黑，一切不通。
        # 注意措辞要对**双方**都中性——告诉被拉黑方「你被拉黑了」等于泄露信息。
        if self.is_blocked(a, b):
            return {
                "reason": RefusalReason.BLOCKED.value,
                "peer": b,
                "needScope": s.value,
                "detail": "该成员当前不可达",
            }
        # owner 对自己的 agent 天然全权——它是你的，不是需要加好友的邻居
        if self._is_owner(a, b):
            return None

        if not self.is_friend(a, b):
            # 软模式：非好友也能说话，但拿不到任何执行权（delegate 永远不给）
            if self.mode == "soft" and s is Scope.CHAT:
                return None
            return {
                "reason": RefusalReason.NOT_FRIEND.value,
                "peer": b,
                "needScope": s.value,
                "mode": self.mode,
            }

        # 信任衰减（§7.4）：长期无互动 → 执行类权限按「已过期」处理。
        # 放在 grant 检查**之前**：这样报出来的是「权限过期了」而不是
        # 「你没给过」——两者对调用方下一步该做什么，指向完全不同。
        if s in EXECUTION_SCOPES and self.is_stale(a, b):
            return {
                "reason": RefusalReason.STALE.value,
                "peer": b,
                "needScope": s.value,
                "staleDays": STALE_DAYS,
                "detail": (
                    f"距上次互动已超过 {STALE_DAYS} 天，执行类权限已降级为对话类；"
                    "重新互动，或请对方重新授予"
                ),
            }

        grant = self.grant_from(b, a)
        if not grant.has(s):
            return {
                "reason": RefusalReason.MISSING_SCOPE.value,
                "peer": b,
                "needScope": s.value,
                "granted": [x.value for x in grant.scopes],
            }
        # **群聊只放宽 chat。** 少了这一步，建个群就能驱动别人的 agent。
        if via.startswith("group:") and s not in GROUP_SCOPES:
            return {
                "reason": RefusalReason.GROUP_BOUNDARY.value,
                "peer": b,
                "needScope": s.value,
                "via": via,
                "detail": "群聊通道只放宽 `chat`，执行类权限必须单独授予",
            }
        return None

    def can(
        self, actor: str, peer: str, scope: Scope | str, *, via: str = "direct"
    ) -> bool:
        """``actor`` 能否以 ``scope`` 作用于 ``peer``。

        **这是唯一的判定入口。** 所有端点、会话层、编排器都走这里，
        不得在业务代码里另写条件。``via="group:<convId>"`` 是群聊降级的唯一口子。
        """
        return self.why_not(actor, peer, scope, via=via) is None

    def refusal_data(
        self,
        actor: str,
        peer: str,
        scope: Scope | str,
        *,
        via: str = "direct",
        hint: str = "",
    ) -> dict[str, Any]:
        """把 :meth:`why_not` 包成一份「带指路」的错误载荷。

        §11.2 的要求：拒绝要能指路，而不是含糊的 403。所以这里一定带上
        ``hint``——调用方照着做就能解开，而不是去翻文档。
        """
        why = self.why_not(actor, peer, scope, via=via) or {}
        target = self.normalize(peer)
        data: dict[str, Any] = {"peer": target, "needScope": str(scope), **why}
        data.setdefault(
            "hint",
            hint
            or f'POST /social/requests {{"to":"{target}","message":"为什么想加对方"}}',
        )
        return data

    def delegable(self, actor: str, agent_id: str) -> bool:
        """``actor`` 能否把任务派给 ``agent_id`` 执行。

        就是 ``delegate`` 档的 ``can()`` —— 单独包一层只是为了让调用点
        读起来是「能不能派活」而不是一串 scope 名字。
        """
        return self.can(actor, agent_id, Scope.DELEGATE)

    def refused_agents(self, actor: str, agent_ids: Iterable[str]) -> list[str]:
        """在这批 agent 里，``actor`` **派不动**的那些。用于编排入口快速失败。"""
        out: list[str] = []
        for aid in agent_ids:
            if not self.delegable(actor, aid):
                out.append(str(aid))
        return out

    def visible_contacts(self, viewer: str) -> Optional[set[str]]:
        """通讯录里该显示哪些 agent（返回裸 agent id）。

        - 门禁未启用 → ``None``（不过滤，兼容 v0.3.0）
        - soft 模式 → 全部 agent（能聊，但不能执行）
        - strict 模式 → 只显示好友 + 自己的 agent
        """
        if not self.enabled:
            return None
        me = self.normalize(viewer)
        visible: set[str] = set()
        for mid, m in self._members.items():
            if not m.is_agent or mid == me:
                continue
            if self._is_owner(me, mid) or self.is_friend(me, mid):
                visible.add(mid[len("agent:") :])
        if self.mode == "soft":
            visible |= {
                mid[len("agent:") :] for mid, m in self._members.items() if m.is_agent
            }
        return visible

    # ------------------------------------------------------------------ #
    # 关系变更
    # ------------------------------------------------------------------ #

    def request(
        self,
        frm: str,
        to: str,
        message: str = "",
        scopes: Optional[list[Scope | str]] = None,
        *,
        auto: bool = True,
    ) -> Relationship:
        """发起好友申请。

        ``scopes`` 是**发起方希望对方授予自己的**权限（建议，不是承诺）。
        真正给多少由 :meth:`accept` 决定——申请不能替对方做决定。

        ``auto``（默认开）表示：申请落地后，**按接收方的自主策略**评估一次
        （§6 判定顺序）。接收方没开自动同意时这一步什么都不做，行为与旧版一致。
        """
        a, b = self.normalize(frm), self.normalize(to)
        if a == b:
            raise SocialError("不能向自己发起好友申请")
        if not message or not message.strip():
            raise SocialError("申请理由不能为空（防骚扰，也让对方知道你是谁）")

        # 被拉黑时中性失败：**不透露「你被拉黑了」**，否则等于泄露信息
        if self.is_blocked(a, b):
            raise SocialConflict("申请未送达")

        rel = self._ensure(a, b)
        if rel.state is RelationState.FRIEND:
            raise SocialConflict("你们已经是好友了")
        if rel.state is RelationState.PENDING:
            if rel.requestedBy == b:
                # 双向同时申请 → 双方意向已互相确认，没有第三方需要批准
                return self._become_friends(rel, approver=b, mutual=True)
            raise SocialConflict("已有在途申请，等待对方回应")
        if rel.state is RelationState.REJECTED:
            if not self._cooldown_passed(rel):
                raise SocialConflict("上次申请被拒后仍在冷却期，请稍后再试")
            rel.state = RelationState.NONE

        rel.state = RelationState.PENDING
        rel.requestedBy = a
        rel.requestMessage = sanitize_reason(message)
        rel.requestedScopes = _sorted_scopes(scopes or DEFAULT_FRIEND_SCOPES)
        if not rel.createdAt:
            rel.createdAt = utc_now()
        rel.updatedAt = utc_now()
        self._record("request", a, b, rel.requestedScopes)
        self.save()
        if auto:
            return self._maybe_auto_accept(rel)
        return rel

    def _maybe_auto_accept(self, rel: Relationship) -> Relationship:
        """接收方的自主策略评估（§6 判定顺序）。**申请落地后调用一次。**"""
        if not self.enabled:
            return rel
        requester = self.normalize(rel.requestedBy)
        receiver = rel.other(requester) if rel.has(requester) else rel.pair[0]
        if receiver == requester or not self.policy_of(receiver).accept.enabled:
            return rel  # 没开自动同意：保持普通在途申请，等人类处理
        decision = self.evaluate_incoming(receiver, requester)
        if decision.action == "auto":
            try:
                return self._become_friends(
                    rel,
                    approver=receiver,
                    as_member=receiver,
                    scopes=list(decision.scopes),
                    mode="auto",
                    decision=decision.audit(),
                )
            except SocialNotPermitted:
                # 权限上行闭包不通过 → 越界，转人审（**不**悄悄放行、也不硬拒）
                decision = Decision(
                    "pending",
                    "owned_scopes",
                    "权限上行闭包：自动同意范围内有自己没有的权限，转人审",
                    scopes=decision.scopes,
                )
        if decision.action == "pending":
            self._add_pending(
                owner=self._approval_owner(receiver),
                requester=requester,
                target=receiver,
                decision=decision,
                kind="accept",
                message=rel.requestMessage,
            )
        return rel

    def accept(
        self,
        actor: str,
        peer: str,
        scopes: Optional[list[Scope | str]] = None,
        *,
        as_member: Optional[str] = None,
    ) -> Relationship:
        """同意申请。

        ``scopes`` 是**同意方实际授予对方的**权限。
        默认只给对话类（``peek/chat/invite``）——``delegate`` 必须显式写出来。

        ``as_member`` 让 owner 代表自己的 agent 同意（``accept(owner, peer,
        as_member="codex")``）。
        """
        acting = self.normalize(as_member or actor)
        actor_n = self.normalize(actor)
        if not self._can_act_for(actor_n, acting):
            raise SocialNotPermitted(f"`{actor_n}` 无权代表 `{acting}` 做社交决策")

        other = self.normalize(peer)
        rel = self._rels.get(self._key(acting, other))
        if rel is None or rel.state is not RelationState.PENDING:
            raise SocialConflict("没有待处理的申请")
        if rel.requestedBy == acting:
            raise SocialConflict("不能同意自己发起的申请")
        return self._become_friends(
            rel, approver=actor_n, as_member=acting, scopes=scopes
        )

    def reject(self, actor: str, peer: str, reason: str = "", *, as_member: Optional[str] = None) -> Relationship:
        """拒绝申请。

        ``reason`` 只写进自己的审计，**不告诉对方**——具体的拒绝理由
        （比如「我只接受 verified 标签」）等于暴露绕过路径。
        """
        acting = self.normalize(as_member or actor)
        if not self._can_act_for(self.normalize(actor), acting):
            raise SocialNotPermitted("无权代表该成员做社交决策")
        rel = self._rels.get(self._key(acting, self.normalize(peer)))
        if rel is None or rel.state is not RelationState.PENDING:
            raise SocialConflict("没有待处理的申请")
        rel.state = RelationState.REJECTED
        rel.updatedAt = utc_now()
        self._record("reject", acting, rel.other(acting), note=reason)
        self.save()
        return rel

    def cancel(self, actor: str, peer: str) -> Relationship:
        """撤回自己发出的申请。"""
        a, b = self.normalize(actor), self.normalize(peer)
        rel = self._rels.get(self._key(a, b))
        if rel is None or rel.state is not RelationState.PENDING or rel.requestedBy != a:
            raise SocialConflict("没有由你发起的在途申请")
        rel.state = RelationState.NONE
        rel.requestMessage = ""
        rel.requestedScopes = []
        rel.updatedAt = utc_now()
        self._record("cancel", a, b)
        self.save()
        return rel

    def revoke(self, actor: str, peer: str, *, as_member: Optional[str] = None) -> Relationship:
        """删除好友。

        **不清历史归档**（关系记录留着，state 回 ``none``），只禁止新消息——
        否则审计链就断了。``grants`` 一并清空。

        ``as_member`` 让 owner 代表自己的 agent 解除（与 ``accept`` 同款）。
        """
        a, b = self._resolve_actor(actor, peer, as_member)
        rel = self._rels.get(self._key(a, b))
        if rel is None or rel.state is not RelationState.FRIEND:
            raise SocialConflict("你们不是好友")
        rel.state = RelationState.NONE
        rel.grants = {}
        rel.updatedAt = utc_now()
        self._record("revoke", a, b)
        self.save()
        return rel

    def block(self, actor: str, peer: str, *, as_member: Optional[str] = None) -> Relationship:
        """拉黑。单向，且**只有拉黑方能解除**。"""
        a, b = self._resolve_actor(actor, peer, as_member)
        if a == b:
            raise SocialError("不能拉黑自己")
        rel = self._ensure(a, b)
        rel.blocked[a] = True
        rel.state = RelationState.BLOCKED
        rel.grants = {}
        rel.updatedAt = utc_now()
        self._record("block", a, b)
        self.save()
        return rel

    def unblock(self, actor: str, peer: str, *, as_member: Optional[str] = None) -> Relationship:
        a, b = self._resolve_actor(actor, peer, as_member)
        rel = self._rels.get(self._key(a, b))
        if rel is None or not rel.blocked.get(a):
            raise SocialConflict("你没有拉黑对方")
        rel.blocked.pop(a, None)
        if not any(rel.blocked.values()):
            rel.state = RelationState.NONE
        rel.updatedAt = utc_now()
        self._record("unblock", a, b)
        self.save()
        return rel

    def set_grant(
        self,
        granter: str,
        grantee: str,
        scopes: Iterable[Scope | str],
        *,
        as_member: Optional[str] = None,
    ) -> Relationship:
        """改权限：``granter`` 调整自己给 ``grantee`` 的范围。

        会强制检查**权限上行闭包**——不能授予自己没有的东西。

        ``as_member`` 让 owner 代表自己的 agent 授权。
        **这条是必需的而不是锦上添花**：agent 自己不会去调 CLI，
        没有它，``delegate`` 就永远授不出去（owner 只能替 agent 同意好友，
        却不能替它给执行权——整个「聊天权 ≠ 指挥权」的开关会卡在关位）。
        """
        a, b = self._resolve_actor(granter, grantee, as_member)
        rel = self._rels.get(self._key(a, b))
        if rel is None or rel.state is not RelationState.FRIEND:
            raise SocialConflict("只能调整好友之间的权限")

        wanted = set(_sorted_scopes(scopes))
        ceiling = self.owned_scopes(a)
        excess = wanted - ceiling
        if excess:
            raise SocialNotPermitted(
                "权限上行闭包：不能授予自己没有的权限 "
                f"{sorted(s.value for s in excess)}"
            )
        rel.grants[b] = Grant(
            scopes=_sorted_scopes(wanted), grantedBy=a, note="manual"
        )
        rel.updatedAt = utc_now()
        self._record("grant", a, b, _sorted_scopes(wanted))
        self.save()
        return rel

    def _resolve_actor(
        self, actor: str, peer: str, as_member: Optional[str] = None
    ) -> tuple[str, str]:
        """把 ``(actor, peer, as_member)`` 解析成 ``(acting, peer_n)``。

        owner 代表自己的 agent 时，``acting`` 是 **agent** 而不是 owner——
        关系边挂在 agent 身上。归属校验与 ``accept`` 共用 ``_can_act_for``：
        **只能代表自己的 agent，人类不能代表其他人类。**
        """
        actor_n = self.normalize(actor)
        acting = self.normalize(as_member or actor)
        if acting != actor_n and not self._can_act_for(actor_n, acting):
            raise SocialNotPermitted(f"`{actor_n}` 无权代表 `{acting}` 做社交决策")
        return acting, self.normalize(peer)

    # ------------------------------------------------------------------ #
    # 信任衰减（§7.4）—— 权限是有保鲜期的租约，不是一劳永逸的授予
    # ------------------------------------------------------------------ #

    def touch_interaction(self, a: str, b: str, *, save: bool = True) -> None:
        """记录一次成功互动，刷新 ``lastInteractAt``。信任衰减的唯一依据。

        设计 §7.4 明确要求「每次成功投递时刷新，**不依赖人工操作**」——
        所以要由会话层在投递成功后自动调用，不能指望用户去点「我们聊过了」。
        """
        rel = self._rels.get(self._key(self.normalize(a), self.normalize(b)))
        if rel is None or rel.state is not RelationState.FRIEND:
            return
        rel.lastInteractAt = utc_now()
        rel.updatedAt = rel.lastInteractAt
        if save:
            self.save()

    def _stale_from(self, rel: Relationship, now: Optional[datetime] = None) -> bool:
        stamp = rel.lastInteractAt or rel.updatedAt or rel.createdAt
        if not stamp:
            return False
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - when).days >= STALE_DAYS

    def is_stale(self, a: str, b: str, *, now: Optional[datetime] = None) -> bool:
        """这条边是否已 ``stale``（超过 :data:`STALE_DAYS` 天没互动）。"""
        rel = self._rels.get(self._key(self.normalize(a), self.normalize(b)))
        if rel is None or rel.state is not RelationState.FRIEND:
            return False
        return self._stale_from(rel, now)

    def sweep_stale(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """把 ``stale`` 边的执行类权限**物理降级**掉，并留痕。

        ``why_not`` 里已经按「过期」拒绝了执行类调用（那是安全底线，
        不依赖清扫）；本方法是**卫生工作**：让 ``describe`` / ``GET /social/me``
        看到的 scope 与实际可用的能力一致——否则界面显示「有 delegate」
        而实际调不动，是最难排查的一类 bug。
        """
        if not self.enabled:
            return []
        out: list[dict[str, Any]] = []
        for rel in self._rels.values():
            if rel.state is not RelationState.FRIEND or not self._stale_from(rel, now):
                continue
            dropped: list[str] = []
            for grant in rel.grants.values():
                kept = [s for s in grant.scopes if s not in EXECUTION_SCOPES]
                if len(kept) != len(grant.scopes):
                    dropped.extend(
                        s.value for s in grant.scopes if s in EXECUTION_SCOPES
                    )
                    grant.scopes = _sorted_scopes(kept)
            if not dropped:
                continue
            rel.updatedAt = utc_now()
            a, b = rel.pair
            self._record(
                "decay",
                a,
                b,
                mode="auto",
                decision=(
                    f"stale：距上次互动超过 {STALE_DAYS} 天，"
                    f"执行类权限 {sorted(set(dropped))} 降级为对话类"
                ),
            )
            out.append({"pair": [a, b], "dropped": sorted(set(dropped))})
        if out:
            self.save()
        return out

    # ------------------------------------------------------------------ #
    # 自主交友（§6）：策略、配额、待办、判定
    # ------------------------------------------------------------------ #

    def policy_of(self, ref: str) -> AutonomyPolicy:
        """某成员的自主交友策略。没声明 / 解析不了 → 全关（默认安全）。"""
        m = self._members.get(self.normalize(ref))
        return AutonomyPolicy.parse(m.autonomy if m else None)

    def _today(self, now: Optional[datetime] = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    def _quota(self, mid: str, now: Optional[datetime] = None) -> dict[str, Any]:
        """当日配额计数。**跨日自动归零**——不归零会变成「配额用一次就永久耗尽」。"""
        mid = self.normalize(mid)
        day = self._today(now)
        st = self._quotas.get(mid)
        if not isinstance(st, dict) or st.get("date") != day:
            st = {"date": day, "sent": 0, "accepted": 0}
            self._quotas[mid] = st
        return st

    def quota_used(self, mid: str, kind: str = "sent", *, now: Optional[datetime] = None) -> int:
        return int(self._quota(mid, now).get(kind, 0) or 0)

    def quota_state(self, mid: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
        return dict(self._quota(mid, now))

    def _bump_quota(self, mid: str, kind: str, n: int = 1, now: Optional[datetime] = None) -> None:
        st = self._quota(mid, now)
        st[kind] = int(st.get(kind, 0) or 0) + n

    def _tags_of(self, ref: str) -> list[str]:
        mid = self.normalize(ref)
        tags = list(self.capabilities(mid).get("tags") or [])
        m = self._members.get(mid)
        if m is not None:
            extra = m.metadata.get("tags") if isinstance(m.metadata, dict) else None
            if isinstance(extra, list):
                tags.extend(str(t) for t in extra)
        return sorted({str(t) for t in tags if t})

    # --- 待办（PendingApproval 队列）------------------------------------ #

    def pending_approvals(self, owner: Optional[str] = None) -> list[dict[str, Any]]:
        """待办箱。``owner`` 为空则返回全部（仅内部/审计用）。

        **待办必须落盘**：丢了等于「agent 申请了但没人知道」——
        自主交友里最糟的失败方式（静默失败）。
        """
        want = self.normalize(owner) if owner is not None else None
        out = [
            dict(a)
            for a in self._approvals
            if a.get("state") == "pending" and (want is None or a.get("owner") == want)
        ]
        out.sort(key=lambda d: d.get("ts") or "", reverse=True)
        return out

    def get_approval(self, pid: str) -> Optional[dict[str, Any]]:
        for a in self._approvals:
            if a.get("id") == pid and a.get("state") == "pending":
                return a
        return None

    def _add_pending(
        self,
        *,
        owner: str,
        requester: str,
        target: str,
        decision: Decision,
        kind: str,
        need: str = "",
        message: str = "",
    ) -> dict[str, Any]:
        entry = {
            "id": f"ap-{uuid4().hex[:12]}",
            "state": "pending",
            "ts": utc_now(),
            "kind": kind,  # "request" = 我要申请别人 / "accept" = 别人申请我
            "owner": self.normalize(owner),
            "requester": self.normalize(requester),
            "target": self.normalize(target),
            "policy": decision.policy,
            "detail": decision.detail,
            "need": need,
            "message": sanitize_reason(message, limit=400),
            "scopes": list(decision.scopes),
        }
        self._approvals.append(entry)
        if len(self._approvals) > APPROVAL_LIMIT:
            del self._approvals[:-APPROVAL_LIMIT]
        self._record(
            "pending",
            entry["requester"],
            entry["target"] if kind == "request" else entry["requester"],
            mode="auto",
            note=entry["message"],
            decision=f"{kind} / {decision.audit()}",
        )
        self.save()
        return entry

    def _close_pending(self, pid: str, state: str) -> bool:
        for a in self._approvals:
            if a.get("id") == pid and a.get("state") == "pending":
                a["state"] = state
                a["closedAt"] = utc_now()
                return True
        return False

    def approve_pending(
        self,
        owner: str,
        pid: str,
        scopes: Optional[list[Scope | str]] = None,
    ) -> dict[str, Any]:
        """owner 批准一条自主待办。**这是唯一能让越界自主行为成真的路径。**"""
        entry = self.get_approval(pid)
        if entry is None:
            raise SocialNotFound("待办不存在或已处理")
        me = self.normalize(owner)
        if entry.get("owner") != me and not self._can_act_for(me, entry.get("owner", "")):
            # 把归属人写进消息：多人类部署下，本地 CLI 默认身份是 `human:default`
            # （合成身份、零权限），只说「你无权」会让人以为功能坏了。
            raise SocialNotPermitted(
                f"只有该待办的归属人才能批准（归属人：{entry.get('owner')}）"
            )
        kind = entry.get("kind")
        wanted = _sorted_scopes(scopes if scopes is not None else entry.get("scopes") or [])
        decision = decide_approve(self.policy_of(me), [s.value for s in wanted])

        if kind == "request":
            rel = self.request(
                entry["requester"],
                entry["target"],
                message=entry.get("message") or "自主申请",
                scopes=wanted,
            )
            self._bump_quota(entry["requester"], "sent")
        else:  # "accept"
            rel = self.accept(
                me,
                entry["requester"],
                scopes=wanted or None,
                as_member=entry["target"] if entry["target"] != me else None,
            )
            self._bump_quota(entry["target"] if entry["target"] != me else me, "accepted")
        self._close_pending(pid, "approved")
        self._record(
            "approve",
            me,
            entry.get("requester", ""),
            wanted,
            mode="owner-approved",
            note=entry.get("message") or "",
            decision=decision.audit(),
        )
        self.save()
        return {"approval": {**entry, "state": "approved"}, "relation": self._rel_json(rel)}

    def deny_pending(self, owner: str, pid: str, reason: str = "") -> dict[str, Any]:
        entry = self.get_approval(pid)
        if entry is None:
            raise SocialNotFound("待办不存在或已处理")
        me = self.normalize(owner)
        if entry.get("owner") != me and not self._can_act_for(me, entry.get("owner", "")):
            raise SocialNotPermitted(
                f"只有该待办的归属人才能驳回（归属人：{entry.get('owner')}）"
            )
        self._close_pending(pid, "denied")
        self._record(
            "deny",
            me,
            entry.get("requester", ""),
            mode="owner-approved",
            note=sanitize_reason(reason),
            decision=f"owner 驳回：{entry.get('policy', '')}",
        )
        self.save()
        return {"approval": {**entry, "state": "denied"}}

    @staticmethod
    def _rel_json(rel: Relationship) -> dict[str, Any]:
        return rel.model_dump(mode="json")

    # --- 判定 ------------------------------------------------------------ #

    def _approval_owner(self, ref: str) -> str:
        """待办该挂到谁那里。

        **关键：挂到「人」而不是「agent」。** agent 没有常驻进程、也不会点
        「同意」；更重要的是，若待办落在 agent 自己名下，它就能自我批准——
        「越界必须人审」（§6 约束 2）当场失效。所以一律上溯到 ``owner``。
        """
        mid = self.normalize(ref)
        return self.effective_owner(mid) or mid

    def evaluate_incoming(
        self, receiver: str, requester: str, *, now: Optional[datetime] = None
    ) -> Decision:
        """申请到达 ``receiver`` 时，按 receiver 的 accept 策略给出决定（§6 判定顺序）。"""
        r, q = self.normalize(receiver), self.normalize(requester)
        rel = self._rels.get(self._key(r, q))
        return decide_accept(
            self.policy_of(r),
            blocked=self.is_blocked(r, q),
            is_friend=self.is_friend(r, q),
            friend_count=len(self.friends_of(r)),
            accepted_today=self.quota_used(r, "accepted", now=now),
            requested=[s.value for s in (rel.requestedScopes if rel else [])],
            requester_kind=self.member_or_synthetic(q).kind,
            requester_tags=self._tags_of(q),
            pending_count=len(self.pending_approvals(owner=self._approval_owner(r))),
        )

    def auto_request(
        self,
        frm: str,
        target: str,
        *,
        need: str = "",
        message: str = "",
        now: Optional[datetime] = None,
    ) -> Decision:
        """按 ``frm`` 的 request 策略决定要不要主动申请 ``target``。

        **只判断，不落库**——调用方拿到 ``Decision`` 再决定怎么执行。
        这样「判定」与「副作用」分开，测试里可以直接断言策略而不用起图。
        """
        me, other = self.normalize(frm), self.normalize(target)
        if me == other:
            # 自己不能申请自己。放在这里而不是 `decide_send` 里：那是纯策略，
            # 不持有 id 语义；「谁是不是谁」属于图的事实。
            return Decision("ignore", "self", "不能向自己发起申请")
        policy = self.policy_of(me)
        rel = self._rels.get(self._key(me, other))
        affinity = self.affinity(me, other, need)["affinity"]
        return decide_send(
            policy,
            blocked=self.is_blocked(me, other),
            is_friend=self.is_friend(me, other),
            pending=bool(rel is not None and rel.state is RelationState.PENDING),
            sent_today=self.quota_used(me, "sent", now=now),
            pending_count=len(self.pending_approvals(owner=me)),
            affinity=affinity,
            want=policy.auto_scope,
            need=need,
            owner=self._approval_owner(me),
        )

    def _need_message(self, need: str, reason: str = "") -> str:
        body = f"我这边有个需求：{need}"
        if reason:
            body += f"（{reason}）"
        return sanitize_reason(body, limit=400)

    def request_for_need(
        self,
        member: str,
        need: str,
        *,
        reason: str = "",
        limit: int = 3,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """§6.1 A：agent 干活时发现干不了 → 用 ``need`` 去发现 → 按策略申请。

        **一轮只挑一个最像的**：一次刷一堆申请既骚扰别人，也把 owner 的
        待办刷成瀑布。剩下的下一轮再说。
        """
        me = self.normalize(member)
        clean = sanitize_reason(need)
        out: dict[str, Any] = {
            "member": me,
            "need": clean,
            "action": "skip",
            "policy": "",
            "detail": "",
            "candidates": [],
            "asked": None,
        }
        if not self.enabled:
            out["detail"] = "社交门禁未启用"
            return out
        if not clean:
            out["detail"] = "need 为空，无法发现"
            return out

        cands = self.discover(me, need=clean, limit=limit)
        out["candidates"] = [{"id": c["id"], "affinity": c["affinity"]} for c in cands]
        for c in cands:
            decision = self.auto_request(
                me, c["id"], need=clean, now=now
            )
            out["policy"] = decision.policy
            out["detail"] = decision.detail
            if decision.action == "skip" or decision.action == "ignore":
                out["action"] = decision.action
                continue
            if decision.action == "silent":
                out["action"] = "silent"
                break
            if decision.action == "pending":
                entry = self._add_pending(
                    owner=decision.owner or self._approval_owner(me),
                    requester=me,
                    target=c["id"],
                    decision=decision,
                    kind="request",
                    need=clean,
                    message=self._need_message(clean, reason),
                )
                self._bump_quota(me, "sent", now=now)
                out["action"] = "pending"
                out["asked"] = {"peer": c["id"], "approval": entry["id"]}
                break
            # action == "send"
            try:
                rel = self.request(
                    me,
                    c["id"],
                    message=self._need_message(clean, reason),
                    scopes=decision.scopes,
                )
            except (SocialError, ValueError) as exc:
                out["action"] = "skip"
                out["detail"] = f"申请未送出：{exc}"
                continue
            self._bump_quota(me, "sent", now=now)
            out["action"] = "sent"
            out["asked"] = {"peer": c["id"], "state": rel.state.value}
            break
        return out

    # --- 巡航 ------------------------------------------------------------ #

    def autonomy_members(self) -> list[str]:
        """开了 ``autonomy.request.enabled`` 的成员（巡航的候选集）。"""
        if not self.enabled:
            return []
        return [
            m.id
            for m in self._members.values()
            if m.autonomy and AutonomyPolicy.parse(m.autonomy).request.enabled
        ]

    def cruise_once(
        self, member: str, *, per_round: int = 2, now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        """替一个成员跑一轮「发现 → 打分 → 申请」。返回本轮实际动作。

        预算是**硬约束**：超了就停，不做「再试一个」。巡航失控时用户第一个
        察觉的不是错误日志，而是别人收到的一堆陌生申请。
        """
        me = self.normalize(member)
        policy = self.policy_of(me)
        if not policy.request.enabled or not self.enabled:
            return []
        goal = policy.request.goal or ""
        acted: list[dict[str, Any]] = []
        for c in self.discover(me, need=goal, limit=max(per_round * 3, 3)):
            if len(acted) >= per_round:
                break
            decision = self.auto_request(me, c["id"], need=goal, now=now)
            if decision.action == "send":
                try:
                    self.request(
                        me,
                        c["id"],
                        message=self._need_message(goal or "想认识一下"),
                        scopes=decision.scopes,
                    )
                except (SocialError, ValueError):
                    continue
                self._bump_quota(me, "sent", now=now)
                acted.append(
                    {"member": me, "peer": c["id"], "action": "send", "policy": decision.policy}
                )
            elif decision.action == "pending":
                entry = self._add_pending(
                    owner=decision.owner or self._approval_owner(me),
                    requester=me,
                    target=c["id"],
                    decision=decision,
                    kind="request",
                    need=goal,
                    message=self._need_message(goal or "想认识一下"),
                )
                self._bump_quota(me, "sent", now=now)
                acted.append(
                    {
                        "member": me,
                        "peer": c["id"],
                        "action": "pending",
                        "policy": decision.policy,
                        "approval": entry["id"],
                    }
                )
        return acted

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _cooldown_passed(self, rel: Relationship) -> bool:
        if not rel.updatedAt:
            return True
        try:
            when = datetime.fromisoformat(rel.updatedAt)
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds() >= REJECT_COOLDOWN

    def _become_friends(
        self,
        rel: Relationship,
        approver: str,
        *,
        as_member: Optional[str] = None,
        scopes: Optional[list[Scope | str]] = None,
        mutual: bool = False,
        mode: str = "human",
        decision: str = "",
    ) -> Relationship:
        """建立边并写入双向授权。

        方向语义（``grants`` 的 key 是**被授予方**）：

        - ``grants[requester]`` ← 同意方给的，默认对话类
        - ``grants[accepter]``  ← 发起方给同意方的，**发起即视为默认授予对话权**
          （加好友的语义本来就是「我们互相可以说话」）

        执行类权限任何一方都要显式给，不会因为「成为好友」而自动产生。
        """
        if mutual:
            left, right = rel.pair
            counterpart = right
            rel.grants[left] = Grant(scopes=list(DEFAULT_FRIEND_SCOPES), grantedBy=right)
            rel.grants[right] = Grant(scopes=list(DEFAULT_FRIEND_SCOPES), grantedBy=left)
            granted_now = list(DEFAULT_FRIEND_SCOPES)
            note = "mutual"
        else:
            accepter = self.normalize(as_member or approver)
            counterpart = self.normalize(rel.requestedBy or rel.pair[0])
            granted_now = (
                _sorted_scopes(scopes)
                if scopes is not None
                else list(DEFAULT_FRIEND_SCOPES)
            )
            ceiling = self.owned_scopes(accepter)
            excess = set(granted_now) - ceiling
            if excess:
                raise SocialNotPermitted(
                    "权限上行闭包：不能授予自己没有的权限 "
                    f"{sorted(s.value for s in excess)}"
                )
            # 同意方 → 发起方：给多少由同意方决定
            rel.grants[counterpart] = Grant(scopes=granted_now, grantedBy=accepter)
            # 发起方 → 同意方：**发起即视为默认授予对话权**（加好友本来就是互相能说话）
            rel.grants[accepter] = Grant(
                scopes=list(DEFAULT_FRIEND_SCOPES), grantedBy=counterpart
            )
            note = ""

        rel.state = RelationState.FRIEND
        rel.updatedAt = utc_now()
        rel.lastInteractAt = rel.updatedAt
        self._record(
            "accept", approver, counterpart, granted_now, note=note, mode=mode, decision=decision
        )
        self.save()
        return rel

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #

    def relations_of(self, ref: str, *, states: Optional[set[RelationState]] = None) -> list[dict[str, Any]]:
        mid = self.normalize(ref)
        out: list[dict[str, Any]] = []
        for rel in self._rels.values():
            if not rel.has(mid):
                continue
            if states is not None and rel.state not in states:
                continue
            out.append(self.describe(rel, mid))
        out.sort(key=lambda d: d.get("updatedAt") or "", reverse=True)
        return out

    def describe(self, rel: Relationship, viewer: str) -> dict[str, Any]:
        """从 ``viewer`` 视角描述一条边。

        ``peer`` 是对方；``myScopes`` 是**我能对他做什么**；
        ``theirScopes`` 是**他能对我做什么**。两个方向分开列，
        因为权限是单向的，合并成一个列表会看不出谁让谁干活。
        """
        me = self.normalize(viewer)
        peer = rel.other(me) if rel.has(me) else rel.pair[0]
        blocked_by_me = bool(rel.blocked.get(me))
        blocked_me = any(v for k, v in rel.blocked.items() if k != me)
        return {
            "peer": peer,
            "peerName": self.name_of(peer) or peer,
            "state": rel.state.value,
            "requestedBy": rel.requestedBy,
            "requestMessage": rel.requestMessage,
            "requestedScopes": [s.value for s in rel.requestedScopes],
            "myScopes": [s.value for s in self.grant_from(peer, me).scopes],
            "theirScopes": [s.value for s in self.grant_from(me, peer).scopes],
            "iBlocked": blocked_by_me,
            "blockedByPeer": blocked_me and not blocked_by_me,
            "lastInteractAt": rel.lastInteractAt,
            "updatedAt": rel.updatedAt,
        }

    def inbox(self, ref: str) -> list[dict[str, Any]]:
        """我收到的、待我处理的申请。"""
        mid = self.normalize(ref)
        out = [
            self.describe(rel, mid)
            for rel in self._rels.values()
            if rel.state is RelationState.PENDING
            and rel.has(mid)
            and rel.requestedBy != mid
        ]
        out.sort(key=lambda d: d.get("updatedAt") or "", reverse=True)
        return out

    def outbox(self, ref: str) -> list[dict[str, Any]]:
        """我发出、对方还没回应的申请。"""
        mid = self.normalize(ref)
        out = [
            self.describe(rel, mid)
            for rel in self._rels.values()
            if rel.state is RelationState.PENDING
            and rel.has(mid)
            and rel.requestedBy == mid
        ]
        out.sort(key=lambda d: d.get("updatedAt") or "", reverse=True)
        return out

    def search(
        self, q: str = "", *, viewer: Optional[str] = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """搜索可发现成员。

        ``viewer`` 给了就按**三档可见性**逐条判定（``private`` 谁都搜不到、
        ``circle`` 限度 ≤ 2、``public`` 人人可见，被引荐的例外）；
        不给则退化为旧行为「只排除 ``private``」，供无身份的本地/测试场景用。

        私有成员不出现在任何他人的搜索结果里——这是「扩大圈层」与
        「不被扫库」之间的取舍点。
        """
        needle = (q or "").strip().lower()
        out: list[dict[str, Any]] = []
        for m in self._members.values():
            if viewer is not None:
                if not self.visible_to(viewer, m.id):
                    continue
            elif m.discoverable == "private":
                continue
            if needle and not (
                needle in m.id.lower()
                or needle in m.name.lower()
                or needle in m.bio.lower()
            ):
                continue
            card = self.card(m)
            if viewer is not None:
                card = {**card, **self.affinity(viewer, m.id)}
            out.append(card)
            if len(out) >= limit:
                break
        if viewer is not None:
            out.sort(key=lambda d: d.get("affinity", 0.0), reverse=True)
        return out

    def card(self, m: Member) -> dict[str, Any]:
        return {
            "id": m.id,
            "name": m.display_name(),
            "kind": m.kind,
            "owner": m.owner,
            "bio": m.bio,
            "discoverable": m.discoverable,
            "autonomous": bool(m.autonomy.get("request", {}).get("enabled")),
        }

    def me(self, ref: str) -> dict[str, Any]:
        """当前成员的完整社交状态（``GET /social/me``）。"""
        mid = self.normalize(ref)
        m = self.member_or_synthetic(mid)
        relations = self.relations_of(mid)
        return {
            "member": {
                **self.card(m),
                "owner": m.owner or self.effective_owner(mid),
                "maxScopes": [s.value for s in (m.max_scopes or [])],
                "ownedScopes": sorted(s.value for s in self.owned_scopes(mid)),
            },
            "enabled": self.enabled,
            "mode": self.mode,
            "friends": [d for d in relations if d["state"] == "friend"],
            "pendingIn": [d for d in relations if d["state"] == "pending" and d["requestedBy"] != mid],
            "pendingOut": [d for d in relations if d["state"] == "pending" and d["requestedBy"] == mid],
            "blocked": [d for d in relations if d["state"] == "blocked"],
            "counts": {
                "friends": sum(1 for d in relations if d["state"] == "friend"),
                "pendingIn": sum(
                    1 for d in relations if d["state"] == "pending" and d["requestedBy"] != mid
                ),
                "pendingOut": sum(
                    1 for d in relations if d["state"] == "pending" and d["requestedBy"] == mid
                ),
                "blocked": sum(1 for d in relations if d["state"] == "blocked"),
            },
        }

    def warn_undeclared_owners(self) -> list[str]:
        """多人类场景下没声明 owner 的 agent —— 启动时提醒一次。

        它们拿不到执行类权限，等于谁都使唤不动。这个静默降级太难排查，
        所以宁可在启动日志里唠叨一句。
        """
        if len([h for h in self._members.values() if h.kind == "human"]) <= 1:
            return []
        return [
            m.id
            for m in self._members.values()
            if m.kind == "agent" and not m.owner
        ]

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #

    def _record(
        self,
        action: str,
        actor: str,
        peer: str,
        scopes: Optional[list[Scope]] = None,
        *,
        note: str = "",
        mode: str = "human",
        decision: str = "",
    ) -> None:
        """关系变化一律留痕。没有「静默加好友」的路径。"""
        self._audit.append(
            {
                "ts": utc_now(),
                "action": action,
                "actor": actor,
                "peer": peer,
                "scopes": [s.value for s in (scopes or [])],
                "mode": mode,
                "note": note,
                "decision": decision,
            }
        )
        if len(self._audit) > AUDIT_LIMIT:
            del self._audit[:-AUDIT_LIMIT]

    def trail(self, peer: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        items = self._audit
        if peer:
            p = self.normalize(peer)
            items = [a for a in items if a["peer"] == p or a["actor"] == p]
        return items[-limit:][::-1]

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """读 ``relations.json``。文件不存在或损坏都不该让 Hub 起不来。"""
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("关系文件 %s 读取失败，按空图继续：%s", self.path, exc)
            return
        for item in raw.get("relations") or []:
            try:
                rel = Relationship(**item)
            except Exception as exc:  # noqa: BLE001
                log.warning("跳过损坏的关系记录：%s", exc)
                continue
            self._rels[tuple(rel.pair)] = rel
        self._audit = list(raw.get("audit") or [])
        # 待审批与人审相关的运行时状态也要跨重启保留：审批待办丢了等于
        # 「agent 申请了但没人知道」，是自主交友里最糟的失败方式。
        self._approvals = list(raw.get("approvals") or [])
        self._quotas = dict(raw.get("quotas") or {})
        log.info(
            "关系图已加载：%d 条边（%d 好友）",
            len(self._rels),
            sum(1 for r in self._rels.values() if r.state is RelationState.FRIEND),
        )

    def save(self) -> None:
        """写盘。先写临时文件再 replace，避免半截文件。"""
        if self.path is None:
            return
        with self._lock:
            payload = {
                "version": 1,
                "relations": [r.model_dump(mode="json") for r in self._rels.values()],
                "audit": self._audit,
                "approvals": self._approvals,
                "quotas": self._quotas,
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("关系文件写入失败 %s：%s", self.path, exc)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def load_members(cls, path: Optional[Path | str]) -> list[Member]:
        """读 ``members.yaml``。文件不存在返回空列表（→ 门禁自动关闭）。"""
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        import yaml

        from .config import expand_env

        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = expand_env(raw)
        out: list[Member] = []
        for item in raw.get("members") or []:
            try:
                m = Member(**item)
            except Exception as exc:  # noqa: BLE001
                log.warning("跳过非法的成员声明 %s：%s", item, exc)
                continue
            # `${VAR}` 没设时会展开成空串。留着它会让 any_tokens() 误判成
            # 「配了 token」，然后把所有人挡在门外，所以在这里就丢掉。
            m.tokens = [t for t in m.tokens if t and t.strip()]
            out.append(m)
        return out

    # ------------------------------------------------------------------ #
    # 就地重载 ——「不重启就能启用社交层」的实现基础
    # ------------------------------------------------------------------ #

    def reload_members(self, members: list[Member]) -> "SocialGraph":
        """就地替换成员表并重算 :attr:`enabled`，**对象引用保持不变**。

        为什么是「就地改」而不是「重建一个 ``SocialGraph``」：同一个图对象
        被五处持有——编排器的 delegate 门禁、``SocialHub``、dispatcher、自主
        交友巡航，以及社交简报 resolver 的闭包。重建就得逐个换引用，漏一处
        就会出现「调用方看到的状态」与「真实状态」不一致。就地改则所有持有
        方下一次访问自动生效，「生成配置 → 立刻可用」才成立。

        - 新成员**覆盖同名、不删除旧成员**：不清空是为了避免已建立的关系
          悬空（旧成员身上可能已经挂着好友边）；
        - 空 token 在这里同样丢掉，避免 ``${VAR}`` 未设时把部署者锁在门外。
        """
        with self._lock:
            for m in members or []:
                m.tokens = [t for t in m.tokens if t and t.strip()]
                self._members[m.id] = m
            self.enabled = bool(self._members) and self.mode != "off"
        return self


# --------------------------------------------------------------------------- #
# 社交简报（把社交属性绑定到 prompt 型 agent 的主通道）
# --------------------------------------------------------------------------- #

#: 简报里最多列多少好友——prompt 是要进上下文窗口的，不能无节制地灌
BRIEFING_MAX_FRIENDS = 8
#: 每个好友最多列几个技能名
BRIEFING_MAX_SKILLS = 4


def social_briefing(graph: "SocialGraph", agent_id: str) -> str:
    """给 agent 的 prompt 前生成一段「社交简报」。

    WorkBuddy / Codex / Claude Code 这类 agent 在 Hub 里只是被 adapter 唤起的
    子进程，**不会也不能自己去调社交 API**——它唯一的感知渠道就是收到的
    prompt。所以「绑定社交属性」不靠改 agent，靠的是执行前把三件事讲给它听：

    1. 你是谁（id + 归属人）；
    2. 你有哪些好友、各自擅长什么（找帮手先找熟人）；
    3. 缺能力时怎么发声（``{"social": {...}}`` 信号协议）+ 行为纪律。

    - 社交层未启用 → 返回空串（prompt 逐字节不变，老约束不破）；
    - 没有好友 → 只讲身份与信号协议，**不虚构朋友**；
    - 每次执行实时生成，好友关系变化下一轮立即生效。
    """
    if not graph.enabled:
        return ""
    me = graph.normalize(agent_id)
    m = graph.get_member(me)
    if m is None:
        return ""
    owner = graph.effective_owner(me)
    friends = graph.friends_of(me)[:BRIEFING_MAX_FRIENDS]
    lines = [
        "[社交简报 · A2A Hub]",
        f"你是 A2A Hub 里的智能体「{graph.name_of(me) or me}」（{me}）"
        + (f"，归属人 {owner}" if owner else "")
        + "。",
    ]
    if friends:
        lines.append("你的好友（可直接对话协作）：")
        for fid in friends:
            cap = graph.capabilities(fid)
            skills: list[str] = []
            for s in cap.get("skills", []):
                # server 路径给 dict（AgentSkill），CLI 进程内给拼好的字符串
                if isinstance(s, dict):
                    name = str(s.get("name") or s.get("id") or "")
                else:
                    name = str(s).split(" ")[0]
                if name:
                    skills.append(name)
                if len(skills) >= BRIEFING_MAX_SKILLS:
                    break
            desc = f"，擅长：{'、'.join(skills)}" if skills else ""
            lines.append(f"- {cap.get('name') or fid}（{fid}）{desc}")
    else:
        lines.append("你还没有好友。遇到需要他人能力的场合按下面的方式发声，Hub 会替你交涉结识。")
    lines += [
        "若任务超出你的能力，在正常回复之外附一行 JSON："
        '{"social": {"need": "<能力名>", "reason": "<为什么需要>"}}，'
        "Hub 会据此帮你寻找、结识具备该能力的伙伴（申请须经对方归属人批准）。",
        "纪律：不要虚构好友或假冒他人；对非好友没有指挥权。",
        "[社交简报结束]",
    ]
    return "\n".join(lines)


__all__ = [
    "AFFINITY_WEIGHTS",
    "APPROVAL_LIMIT",
    "AUDIT_LIMIT",
    "AutonomyPolicy",
    "BRIEFING_MAX_FRIENDS",
    "BRIEFING_MAX_SKILLS",
    "DEFAULT_FRIEND_SCOPES",
    "DEFAULT_MEMBER",
    "DISTANCE_UNREACHABLE",
    "Decision",
    "EXECUTION_SCOPES",
    "FOF_SATURATION",
    "GROUP_SCOPES",
    "Grant",
    "Member",
    "MEMBER_PREFIXES",
    "NullGuard",
    "PRIVILEGED_SCOPES",
    "REJECT_PENALTY_DAYS",
    "Relationship",
    "RelationState",
    "RefusalReason",
    "SCOPE_ORDER",
    "STALE_DAYS",
    "Scope",
    "SocialConflict",
    "SocialError",
    "SocialGraph",
    "SocialNotPermitted",
    "SocialNotFound",
    "FriendshipGuard",
    "REJECT_COOLDOWN",
    "SYSTEM",
    "USER",
    "social_briefing",
]
