"""自主交友策略（设计 §6）—— **纯策略，不碰状态**。

这一层刻意不 import ``relations`` / ``registry`` / ``social``：它只做
「给定策略 + 一组事实 → 给出决定」的纯函数运算。状态（关系图、配额、
待办）全在 :class:`a2a_hub.relations.SocialGraph` 里。这样策略可以单测，
也不用担心社交层的循环依赖。

三条硬约束（照抄设计 §6，因为它们是本模块存在的理由）
----------------------------------------------------
1. **权限上行闭包**：``grant(A→B) ⊆ owned_scopes(A)``。由 relations 层执行，
   本层只负责**不主动**把越界的东西放进决定里。
2. **越界必须人审**：任何「不确定该不该同意」的情形一律落到 ``pending``，
   交给 owner 拍板——**绝不**为了省事自动拒绝或自动放行。
3. **一切留痕**：每个 :class:`Decision` 都带 ``policy`` 字段，指名命中了
   哪条规则。事后要能区分「策略太松」和「实现有 bug」，光记一个结果不够。

一条自己加的安全不变量
----------------------
**自主同意永不授出执行类权限**（``delegate`` / ``artifact`` / ``admin``）。
哪怕 ``accept.maxScope`` 里写了，也会被 :data:`EXECUTION_SCOPES` 削掉。
理由：执行类权限意味着「能让对方的 agent 替你干活」，让它由一次冷淡的
自动同意产生，等于把 §6 的约束 2 架空了。这类权限必须有人在环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional

log = logging.getLogger("a2a_hub.autonomy")

#: 对话类权限：默认给好友的档位，也是 ``accept.maxScope`` 的默认值。
DEFAULT_MAX_SCOPE: tuple[str, ...] = ("peek", "chat", "invite")

#: 执行类权限：**任何自主路径都不得自动授出**，必须人批（见模块 docstring）。
EXECUTION_SCOPES: frozenset[str] = frozenset({"delegate", "artifact", "admin"})

#: 信任保鲜期（天）。超过 → 该边 ``stale``，执行类权限降级回对话类（§7.4）。
STALE_DAYS = 90

#: 自主社交通道的日预算上限（防止配置写个天文数字把网络刷爆）。
MAX_DAILY_QUOTA = 50
#: 单个成员在途待办的上限（防止 owner 的待办被刷成瀑布）。
MAX_PENDING_CAP = 50

#: 申请理由 / need 文本的长度上限与清洗（§13 风险 9：理由会进审批界面）。
REASON_LIMIT = 200
#: 指令性标记，去掉以防「申请理由」被当成给审批方 LLM 的指令。
_INJECTION_RE = re.compile(r"(?i)(ignore\s+(all\s+)?previous|system\s*:|<\|.*?\|>|```)")

#: 决定动作。
#: ``silent`` 静默丢弃（被拉黑，不能泄露任何信息）
#: ``auto``   自动执行（同意 / 建立关系）
#: ``send``   去发申请
#: ``pending`` 挂到 owner 待办，等人批
#: ``skip``   本轮不做（配额 / 阈值不满足），但对方向后仍可正常申请
#: ``ignore`` 本层不介入（自主未开启），退回原有行为
Action = Literal["silent", "auto", "send", "pending", "skip", "ignore"]


def sanitize_reason(text: str, limit: int = REASON_LIMIT) -> str:
    """清洗外部可控的文本（申请理由 / need）。截断 + 去指令性标记。

    申请理由会出现在**审批界面**里。如果审批方是 LLM，它就是一条注入通道
    （§13 风险 9）。所以这里先洗一遍，渲染侧再以纯文本呈现，不喂进指令位。
    """
    clean = (text or "").strip()
    clean = _INJECTION_RE.sub("", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _as_tuple(value: Any) -> tuple[str, ...]:
    """把 YAML 里的 list / 逗号串统一成字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(p.strip() for p in value.split(",") if p.strip())
    if isinstance(value, Iterable):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def _as_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequestPolicy:
    """主动申请侧（§6 约束 2 的 ``autonomy.request``）。"""

    enabled: bool = False
    goal: str = ""
    dailyQuota: int = 3
    maxPending: int = 5
    minAffinity: float = 0.6
    requireOwnerApproval: bool = False


@dataclass(frozen=True)
class AcceptPolicy:
    """自动同意侧（``autonomy.accept``）。"""

    enabled: bool = False
    fromKinds: tuple[str, ...] = ("human", "agent")
    fromTags: tuple[str, ...] = ()
    maxScope: tuple[str, ...] = DEFAULT_MAX_SCOPE


@dataclass(frozen=True)
class Limits:
    """社交半径上限（``autonomy.limits``）。"""

    maxFriends: int = 50
    maxGroups: int = 20
    minAffinity: float = 0.6


@dataclass(frozen=True)
class AutonomyPolicy:
    """一个成员的自主交友策略。缺省全部关闭——**默认关，必须显式打开**。"""

    request: RequestPolicy = field(default_factory=RequestPolicy)
    accept: AcceptPolicy = field(default_factory=AcceptPolicy)
    limits: Limits = field(default_factory=Limits)

    @property
    def any_enabled(self) -> bool:
        return self.request.enabled or self.accept.enabled

    @property
    def auto_scope(self) -> tuple[str, ...]:
        """**真正**可以被自动授出的范围：``maxScope`` 削掉执行类。"""
        return tuple(
            s for s in self.accept.maxScope if s not in EXECUTION_SCOPES
        )

    @classmethod
    def parse(cls, raw: Optional[dict[str, Any]]) -> AutonomyPolicy:
        """从 ``Member.autonomy`` 的原始 dict 解析。**任何异常输入都不该抛。**"""
        if not isinstance(raw, dict):
            return cls()
        req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        acc = raw.get("accept") if isinstance(raw.get("accept"), dict) else {}
        lim = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
        return cls(
            request=RequestPolicy(
                enabled=_as_bool(req.get("enabled")),
                goal=sanitize_reason(str(req.get("goal") or "")),
                dailyQuota=_as_int(req.get("dailyQuota"), 3, lo=0, hi=MAX_DAILY_QUOTA),
                maxPending=_as_int(req.get("maxPending"), 5, lo=0, hi=MAX_PENDING_CAP),
                minAffinity=_as_float(req.get("minAffinity"), 0.6),
                requireOwnerApproval=_as_bool(req.get("requireOwnerApproval")),
            ),
            accept=AcceptPolicy(
                enabled=_as_bool(acc.get("enabled")),
                fromKinds=_as_tuple(acc.get("fromKinds")) or ("human", "agent"),
                fromTags=_as_tuple(acc.get("fromTags")),
                maxScope=_as_tuple(acc.get("maxScope")) or DEFAULT_MAX_SCOPE,
            ),
            limits=Limits(
                maxFriends=_as_int(lim.get("maxFriends"), 50, lo=0, hi=10_000),
                maxGroups=_as_int(lim.get("maxGroups"), 20, lo=0, hi=10_000),
                minAffinity=_as_float(lim.get("minAffinity"), 0.6),
            ),
        )


# --------------------------------------------------------------------------- #
# 决定
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decision:
    """一次自主判定。``policy`` 是审计的可解释性落点（§6 约束 3）。"""

    action: Action
    policy: str = ""
    detail: str = ""
    scopes: tuple[str, ...] = ()
    need: str = ""
    #: 为 ``pending`` 记录：该待办挂到谁的待办箱里。
    owner: str = ""

    @property
    def autonomous(self) -> bool:
        return self.action in ("auto", "send")

    def audit(self) -> str:
        return f"{self.policy}：{self.detail}" if self.detail else self.policy

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "policy": self.policy,
            "detail": self.detail,
            "scopes": list(self.scopes),
            "need": self.need,
            "owner": self.owner,
        }


# --------------------------------------------------------------------------- #
# 判定顺序（§6：短路，从上往下）
# --------------------------------------------------------------------------- #


def decide_send(
    policy: AutonomyPolicy,
    *,
    blocked: bool = False,
    is_friend: bool = False,
    pending: bool = False,
    sent_today: int = 0,
    pending_count: int = 0,
    affinity: float = 0.0,
    want: Iterable[str] = DEFAULT_MAX_SCOPE,
    need: str = "",
    owner: str = "",
) -> Decision:
    """主动申请侧的判定。"""
    if blocked:
        return Decision("silent", "blocked", "被拉黑：静默丢弃，不泄露任何信息")
    if not policy.request.enabled:
        return Decision("ignore", "request.enabled", "未开启自主申请")
    if is_friend:
        return Decision("ignore", "already-friend", "已经是好友")
    if pending:
        return Decision("ignore", "pending", "已有在途申请")
    if pending_count >= policy.request.maxPending:
        return Decision(
            "skip",
            "request.maxPending",
            f"在途申请已达上限 {policy.request.maxPending}",
        )
    if sent_today >= policy.request.dailyQuota:
        return Decision(
            "skip",
            "request.dailyQuota",
            f"今日自主申请配额已用尽（{sent_today}/{policy.request.dailyQuota}）",
        )
    if affinity < policy.request.minAffinity:
        return Decision(
            "skip",
            "request.minAffinity",
            f"匹配度 {affinity:.2f} 低于阈值 {policy.request.minAffinity}",
        )
    # 申请只**请求**对话类；执行类一律不自动张嘴（对方也不会自动给）
    ask = tuple(s for s in (_as_tuple(want) or DEFAULT_MAX_SCOPE) if s not in EXECUTION_SCOPES)
    if policy.request.requireOwnerApproval:
        return Decision(
            "pending",
            "request.requireOwnerApproval",
            "策略要求连发申请都需人批",
            scopes=ask,
            need=need,
            owner=owner,
        )
    return Decision(
        "send",
        "request.enabled + request.minAffinity",
        f"匹配度 {affinity:.2f} ≥ {policy.request.minAffinity}",
        scopes=ask,
        need=need,
        owner=owner,
    )


def decide_accept(
    policy: AutonomyPolicy,
    *,
    blocked: bool = False,
    is_friend: bool = False,
    friend_count: int = 0,
    accepted_today: int = 0,
    requested: Iterable[str] = (),
    requester_kind: str = "human",
    requester_tags: Iterable[str] = (),
    pending_count: int = 0,
) -> Decision:
    """自动同意侧的判定：**越界一律转人审**（§6 约束 2）。"""
    if blocked:
        return Decision("silent", "blocked", "被拉黑：静默丢弃")
    if not policy.accept.enabled:
        return Decision("ignore", "accept.enabled", "未开启自动同意")
    if is_friend:
        return Decision("ignore", "already-friend", "已经是好友")
    if friend_count >= policy.limits.maxFriends:
        return Decision(
            "pending",
            "limits.maxFriends",
            f"好友数已达上限 {policy.limits.maxFriends}，建议先清理最久未互动的",
        )
    if accepted_today >= policy.request.dailyQuota:
        return Decision(
            "pending",
            "request.dailyQuota",
            f"今日自主同意配额已用尽（{accepted_today}/{policy.request.dailyQuota}）",
        )
    if pending_count >= policy.request.maxPending:
        return Decision(
            "pending",
            "request.maxPending",
            f"待办已积压 {pending_count} 条，先处理再看新的",
        )
    if requester_kind not in policy.accept.fromKinds:
        return Decision(
            "pending",
            "accept.fromKinds",
            f"来源类型 {requester_kind} 不在 {list(policy.accept.fromKinds)} 内",
        )
    if policy.accept.fromTags and not (
        set(_as_tuple(requester_tags)) & set(policy.accept.fromTags)
    ):
        return Decision(
            "pending",
            "accept.fromTags",
            f"来源标签不满足 {list(policy.accept.fromTags)}",
        )
    allowed = set(policy.auto_scope)  # 执行类已被削掉
    wanted = set(_as_tuple(requested)) or set(allowed)
    if wanted - allowed:
        return Decision(
            "pending",
            "accept.maxScope",
            f"请求范围 {sorted(wanted - allowed)} 超出可自动同意的 {sorted(allowed)}",
        )
    if policy.request.requireOwnerApproval:
        return Decision(
            "pending",
            "request.requireOwnerApproval",
            "策略要求连同意都需人批",
            scopes=tuple(sorted(wanted)),
        )
    return Decision(
        "auto",
        "accept.enabled + accept.maxScope",
        f"请求范围 {sorted(wanted)} ⊆ 可自动同意范围 {sorted(allowed)}，"
        f"来源 {requester_kind} 通过 fromKinds {list(policy.accept.fromKinds)}",
        scopes=tuple(sorted(wanted)),
    )


def decide_approve(policy: AutonomyPolicy, requested: Iterable[str]) -> Decision:
    """owner 批准一条待办时，检查是否仍在「可自动的范围」内。

    人工批准本身是最高授权，所以这里**只做提示不做拦截**——但会把
    越界事实记进审计，免得「owner 批了」变成无声的例外。
    """
    allowed = set(policy.auto_scope)
    wanted = set(_as_tuple(requested))
    extra = wanted - allowed
    if extra:
        return Decision(
            "auto",
            "owner-approved (超出 accept.maxScope)",
            f"owner 人工批准，含超出自动范围的 {sorted(extra)}",
            scopes=tuple(sorted(wanted)),
        )
    return Decision(
        "auto",
        "owner-approved",
        f"owner 人工批准 {sorted(wanted)}",
        scopes=tuple(sorted(wanted)),
    )


# --------------------------------------------------------------------------- #
# 任务内信号（§6.1 A 主路径）
# --------------------------------------------------------------------------- #

#: 匹配文本里嵌着的 ``{"social": {...}}``。刻意限制成「不含嵌套花括号」——
#: 递归解析 JSON 片段是另一个量级的复杂度，而信号格式是**我们规定的**。
_SIGNAL_RE = re.compile(r'\{\s*"social"\s*:\s*\{[^{}]*\}\s*\}')


def extract_need(payload: Any) -> list[dict[str, str]]:
    """从 agent 的产出里抽出 ``{"social": {"need": ..., "reason": ...}}`` 信号。

    兼容两种来源：

    - 结构化 ``data`` part（dict）——正常路径；
    - 纯文本里嵌的 JSON 片段——CLI 型 agent 只能吐文本，兜底用。

    没信号就返回空列表（**绝不猜**）。
    """
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload)
    elif isinstance(payload, str):
        for m in _SIGNAL_RE.finditer(payload):
            try:
                candidates.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                continue

    out: list[dict[str, str]] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        sig = cand.get("social")
        if not isinstance(sig, dict):
            continue
        need = sanitize_reason(str(sig.get("need") or ""))
        if not need:
            continue
        out.append({"need": need, "reason": sanitize_reason(str(sig.get("reason") or ""))})
    return out


def parse_signals(text: str) -> list[dict[str, str]]:
    """纯文本版的 :func:`extract_need`（给 CLI 型 agent 的产出用）。"""
    return extract_need(text)


# --------------------------------------------------------------------------- #
# 社交巡航（§6.1 B 辅路径）
# --------------------------------------------------------------------------- #


class SocialCruise:
    """后台巡航：定期替「开了自主申请」的成员跑一轮「发现 → 打分 → 申请」。

    **默认关。** 三条硬性要求（设计 §6.1 B 明写）：

    - 全局开关 ``A2A_AUTONOMY_ENABLED``，默认关；
    - 必须能在测试里关掉，否则测试会随机发起网络请求；
    - 有预算：每轮最多 N 条、每天最多 M 条，超了就停。

    ``graph`` 是鸭子类型：只要有 ``autonomy_members()`` 和
    ``cruise_once(member, per_round=...)`` 两个方法就行——本模块因此
    不需要 import ``relations``。
    """

    def __init__(
        self,
        graph: Any,
        *,
        enabled: bool = False,
        interval: float = 300.0,
        per_round: int = 2,
        daily: int = 6,
    ) -> None:
        self.graph = graph
        self.enabled = bool(enabled)
        self.interval = max(1.0, float(interval))
        self.per_round = max(1, int(per_round))
        self.daily = max(0, int(daily))
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop: Optional[asyncio.Event] = None
        self._day = ""
        self._spent = 0
        self.rounds = 0
        self.last_result: list[dict[str, Any]] = []

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._spent = 0

    async def run_once(self) -> list[dict[str, Any]]:
        """跑一轮。巡航关闭时**直接返回空**，一个请求都不发。"""
        if not self.enabled:
            return []
        self._roll_day()
        if self._spent >= self.daily:
            log.info("自主巡航：今日预算已用尽（%d/%d），跳过本轮", self._spent, self.daily)
            return []
        budget = min(self.per_round, self.daily - self._spent)
        out: list[dict[str, Any]] = []
        for member in self.graph.autonomy_members():
            if budget <= 0:
                break
            try:
                acted = self.graph.cruise_once(member, per_round=budget)
            except Exception:  # noqa: BLE001  巡航挂掉不能拖垮 Hub
                log.exception("自主巡航处理 %s 失败", member)
                continue
            out.extend(acted)
            used = sum(1 for a in acted if a.get("action") in ("send", "auto"))
            budget -= used
            self._spent += used
        self.rounds += 1
        self.last_result = out
        return out

    async def _loop(self) -> None:
        assert self._stop is not None
        log.info(
            "自主巡航已启动：每 %.0fs 一轮，每轮 ≤%d，每天 ≤%d",
            self.interval,
            self.per_round,
            self.daily,
        )
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                    break  # 被要求停止
                except asyncio.TimeoutError:
                    pass
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("自主巡航本轮失败，继续下一轮")
        except asyncio.CancelledError:  # pragma: no cover - 关停路径
            pass
        finally:
            log.info("自主巡航已停止")

    def start(self) -> bool:
        """启动巡航。**未开启时返回 False 且什么都不做**（零后台请求）。"""
        if not self.enabled or self.running:
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._task.cancel()
            self._task = None


__all__ = [
    "AutonomyPolicy",
    "AcceptPolicy",
    "RequestPolicy",
    "Limits",
    "Decision",
    "SocialCruise",
    "DEFAULT_MAX_SCOPE",
    "EXECUTION_SCOPES",
    "STALE_DAYS",
    "decide_send",
    "decide_accept",
    "decide_approve",
    "extract_need",
    "parse_signals",
    "sanitize_reason",
]
