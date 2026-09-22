"""A2A Hub 命令行工具。

默认 **进程内** 运行（不需要先起服务，开箱即用）；
加 ``--url http://host:port`` 则走 HTTP 与远端 Hub 通信。

    a2a-hub serve                         启动服务 + Web 控制台
    a2a-hub agents [--refresh]            查看已注册 agent 与健康状态
    a2a-hub card [--agent ID]             打印 Agent Card（A2A 发现机制）
    a2a-hub ask "问题" [--agent ID]       向某个/自动路由的 agent 发起任务
    a2a-hub collab MODE "任务"            发起多 agent 协同
    a2a-hub modes                         查看协同模式
    a2a-hub health                        探测所有 agent 健康

会话层（像微信一样和 agent 聊，会话内上下文自动延续）：
    a2a-hub im contacts                   通讯录
    a2a-hub im open echo                  打开/复用单聊，打印会话 id
    a2a-hub im chat echo "你好"           单聊一步到位：发消息 → 等回复 → 打印
    a2a-hub im group --members a,b "问题"  建群并发问，等所有人回完
    a2a-hub im say -c CONV "追加一句"      继续已有的会话
    a2a-hub im log -c CONV                查看聊天记录与投递回执

社交层（把 agent 当独立个体，加好友才能交流）：
    a2a-hub social me                     我的名片、权限上限与待办
    a2a-hub social find [关键词]          搜索可发现成员
    a2a-hub social profile <成员>         看某人的名片（只有共同好友数，不给名单）
    a2a-hub social discover --need "OCR"  按匹配度发现值得认识的人（带打分明细）
    a2a-hub social add codex --reason "..." [--scopes chat]   发好友申请
    a2a-hub social inbox                  待我处理的申请
    a2a-hub social accept codex [--scopes peek,chat]          同意
    a2a-hub social friends                我的好友
    a2a-hub social grant codex --scopes chat,delegate         调整权限（delegate = 允许派活）
    a2a-hub social revoke codex           删好友（历史归档，单聊转为只读）
    a2a-hub social block codex [--undo]   拉黑 / 解除
    a2a-hub social introduce codex --to guest --note "..."    引荐（不授予任何权限）
    a2a-hub social intro                  别人引荐给我的人
    a2a-hub social audit                  关系变更审计链

自主交友（成员声明 `autonomy` 后才生效；巡航默认关）：
    a2a-hub social need "OCR 表格提取"     报告能力缺口 → 发现 → 申请 / 挂待办
    a2a-hub social approvals              待我拍板的自主交友待办（附命中策略）
    a2a-hub social approve ap-xxxx        批准（越界自主行为成真的唯一路径）
    a2a-hub social deny ap-xxxx --reason "…"   驳回

门禁开启后，`ask` / `collab` / `im` 也会走同一套判定；本地多成员部署用
`--as <成员>` 说明自己是谁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Optional

from .config import get_settings

BANNER = r"""
    _    ___    _      _   _       _
   / \  |__ \  / \    | | | |_   _| |__
  / _ \   / / / _ \   | |_| | | | | '_ \
 / ___ \ / /_/ ___ \  |  _  | |_| | |_) |
/_/   \_\____/_/   \_\ |_| |_|\__,_|_.__/
  Agent-to-Agent Hub · 异构 Agent 互联互通与协同
"""


# --------------------------------------------------------------------------- #
# 输出助手
# --------------------------------------------------------------------------- #

C = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "blue": "\033[34m",
}

# Windows 老终端默认不解析 ANSI，开启 VT 处理
if sys.platform == "win32":
    os.system("")

# Windows 下重定向到文件时 stdout 走本地代码页（GBK），中文与 ●◐✗ 等字符
# 会乱码甚至抛 UnicodeEncodeError；统一强制 UTF-8，保证控制台与 `> out.txt` 一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{C.get(color, '')}{text}{C['reset']}"


STATE_COLOR = {
    "healthy": "green",
    "degraded": "yellow",
    "unavailable": "red",
    "disabled": "dim",
    "unknown": "dim",
}

STATE_ICON = {
    "submitted": "○",
    "working": "◐",
    "input-required": "?",
    "completed": "●",
    "canceled": "◌",
    "failed": "✗",
    "rejected": "✗",
}


def print_agents(agents: list[dict[str, Any]]) -> None:
    if not agents:
        print("（没有已注册的 agent）")
        return
    width = max(len(a["id"]) for a in agents)
    for a in agents:
        h = a.get("health", {})
        status = h.get("status", "unknown")
        dot = c("●", STATE_COLOR.get(status, "dim"))
        print(
            f"{dot} {c(a['id'].ljust(width), 'bold')}  {a['name']}"
            f"  {c('[' + a['type'] + ']', 'dim')}"
        )
        if h.get("detail"):
            print(f"    {c(status, STATE_COLOR.get(status, 'dim'))} · {c(h['detail'], 'dim')}")
        for s in a.get("skills", [])[:3]:
            tags = ",".join(s.get("tags", [])[:5])
            print(f"    ├ {s['name']}  {c(tags, 'cyan')}")
        if len(a.get("skills", [])) > 3:
            print(f"    └ ... 另有 {len(a['skills']) - 3} 项能力")


# --------------------------------------------------------------------------- #
# 会话层（IM）的终端渲染
# --------------------------------------------------------------------------- #

DELIVERY_STYLE = {
    "pending": ("○", "dim"),
    "delivered": ("◔", "cyan"),
    "read": ("◑", "cyan"),
    "replied": ("●", "green"),
    "failed": ("✗", "red"),
}


def _local_hms(ts: str) -> str:
    """把 A2A 的 UTC 时间戳转成本地 ``HH:MM:SS``。

    直接截取字符串会显示 UTC 时刻，用户看到的"发送时间"会比手表慢 8 小时，
    这种细节最容易让人觉得东西是坏的。
    """
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)[11:19]


def print_im_contacts(items: list[dict[str, Any]]) -> None:
    """打印通讯录。"""
    if not items:
        print("（通讯录为空）")
        return
    width = max(len(i["id"]) for i in items)
    for i in items:
        dot = c("●", "green" if i["online"] else "dim")
        print(f"{dot} {c(i['id'].ljust(width), 'bold')}  {i['name']:<10}"
              f"  {c('[' + i['type'] + ']', 'dim')}")
        if not i["online"]:
            print(f"    {c(i['detail'] or '离线', 'dim')}")


def print_im_transcript(conversation: dict[str, Any], messages: list[dict[str, Any]],
                        deliveries: list[dict[str, Any]]) -> None:
    """按聊天窗口的样子打印会话内容（含每条消息的投递回执）。

    形参名刻意与 ``SocialHub.history()`` 的返回键对齐，
    这样调用处可以直接 ``print_im_transcript(**history(...))``。
    """
    who_all = "、".join(m["name"] for m in conversation.get("members", []))
    head = ("与 " + who_all + " 的单聊") if conversation.get("kind") == "direct" \
        else f"群聊「{conversation.get('title')}」"
    print(c(f"── {head} ──", "bold"))
    print(c(f"   会话 {conversation['id']}   上下文 {conversation['contextId']}", "dim"))
    print()

    by_msg: dict[str, list[dict[str, Any]]] = {}
    for d in deliveries:
        by_msg.setdefault(d["messageId"], []).append(d)

    if not messages:
        print(c("   （还没有消息）", "dim"))
        print()
        return

    for m in messages:
        ts = _local_hms(m.get("ts", ""))
        if m.get("kind") == "system":
            print(c(f"           {ts}  {m['text']}", "dim"))
            continue
        mine = m.get("sender") == "user"
        name = m.get("senderName") or m.get("sender", "?")
        label = c(name.ljust(12), "cyan" if mine else "magenta")
        lines = (m.get("text") or "").splitlines() or [""]
        for i, line in enumerate(lines):
            if i == 0:
                print(f"{label}  {c(ts, 'dim')}  {line}")
            else:
                print(f"{' ' * 12}            {line}")
        for d in by_msg.get(m["id"], []):
            icon, color = DELIVERY_STYLE.get(d.get("state", ""), ("·", "dim"))
            note = f"  {c(d['error'], 'dim')}" if d.get("error") else ""
            print(f"{' ' * 12}  └ {c(icon, color)} {c(d.get('agentName', ''), 'dim')}"
                  f" {c(d.get('state', ''), color)}{note}")
    print()


# --------------------------------------------------------------------------- #
# 社交层（好友关系）的终端渲染
# --------------------------------------------------------------------------- #

REL_STYLE = {
    "friend": ("●", "green"),
    "pending": ("◐", "yellow"),
    "rejected": ("✗", "red"),
    "blocked": ("⊘", "red"),
    "none": ("○", "dim"),
}

#: 执行类 scope —— 终端里标黄，因为它们默认**不给**，出现即意味着有人显式授权过。
_PRIVILEGED_NAMES = {"profile", "delegate", "artifact", "admin"}


def _fmt_scopes(scopes: Any) -> str:
    """把 scope 列表渲染成一串。执行类标黄，对话类标青，空用 ``—``。"""
    items = list(scopes or [])
    if not items:
        return c("—", "dim")
    return " ".join(
        c(str(s), "yellow" if str(s) in _PRIVILEGED_NAMES else "cyan") for s in items
    )


def print_social_me(data: dict[str, Any]) -> None:
    """``social me``：我的名片 + 待办数 + 双向权限一览。

    ``myScopes`` 是**我能对对方做什么**，``theirScopes`` 是**对方能对我做什么**；
    两个方向分开列，因为权限是单向的——合并成一列就看不出谁能让谁干活。
    """
    m = data.get("member", {})
    dot = c("●", "green") if data.get("enabled") else c("○", "dim")
    print(f"{dot} {c(m.get('name') or m.get('id', '?'), 'bold')}  "
          f"{c(m.get('id', ''), 'dim')}  {c('[' + str(m.get('kind', '')) + ']', 'dim')}")
    if m.get("bio"):
        print(f"   {c(m['bio'], 'dim')}")
    if m.get("owner"):
        print(f"   归属   {c(m['owner'], 'cyan')}")
    cnt = data.get("counts", {})
    print(f"   好友 {c(str(cnt.get('friends', 0)), 'green')}"
          f" · 待我处理 {c(str(cnt.get('pendingIn', 0)), 'yellow')}"
          f" · 在途 {cnt.get('pendingOut', 0)}"
          f" · 已拉黑 {cnt.get('blocked', 0)}")
    print(f"   可授予上限   {_fmt_scopes(m.get('ownedScopes'))}")
    if not data.get("enabled"):
        print(c("   门禁未启用——所有 agent 默认可达。", "yellow"))
        return
    print(c(f"   模式   {data.get('mode', '?')}", "dim"))

    for title, key, state in (
        ("好友", "friends", "friend"),
        ("待我处理", "pendingIn", "pending"),
        ("我在途的申请", "pendingOut", "pending"),
        ("已拉黑", "blocked", "blocked"),
    ):
        items = data.get(key) or []
        if not items:
            continue
        print()
        print(c(f"── {title}（{len(items)}）──", "bold"))
        for it in items:
            print_social_relation(it, indent=2)


def print_social_relation(it: dict[str, Any], indent: int = 0) -> None:
    """打印一条关系（``describe()`` 的产物）。"""
    pad = " " * indent
    icon, color = REL_STYLE.get(it.get("state", "none"), ("·", "dim"))
    peer = it.get("peer", "?")
    name = it.get("peerName") or peer
    print(f"{pad}{c(icon, color)} {c(peer, 'bold')}  {name}"
          f"  {c(it.get('state', ''), color)}")
    if it.get("requestMessage"):
        who = "对方想加你" if it.get("requestedBy") == it.get("peer") else "你的申请"
        print(f"{pad}    {c(who + '：', 'dim')}{it['requestMessage']}")
    if it.get("requestedScopes"):
        print(f"{pad}    对方想要   {_fmt_scopes(it['requestedScopes'])}")
    print(f"{pad}    我能对他   {_fmt_scopes(it.get('myScopes'))}")
    print(f"{pad}    他能对我   {_fmt_scopes(it.get('theirScopes'))}")
    flags = []
    if it.get("iBlocked"):
        flags.append("我已拉黑")
    if it.get("blockedByPeer"):
        flags.append("被对方拉黑")
    if flags:
        print(f"{pad}    {c(' / '.join(flags), 'red')}")


def print_social_members(items: list[dict[str, Any]], query: str = "") -> None:
    """``social find``：可发现成员列表（私有成员不会出现在这里）。"""
    if not items:
        hint = f"（没有匹配 `{query}` 的可发现成员）" if query else "（没有可发现的成员）"
        print(c(hint, "dim"))
        return
    for m in items:
        flag = c("自主", "magenta") if m.get("autonomous") else ""
        print(f"{c('●', 'cyan')} {c(m['id'], 'bold')}  {m.get('name', '')}  "
              f"{c('[' + str(m.get('kind', '')) + ']', 'dim')}  "
              f"{c(str(m.get('discoverable', '')), 'dim')} {flag}")
        if m.get("bio"):
            print(f"    {c(m['bio'], 'dim')}")
        if m.get("owner"):
            print(f"    归属 {c(m['owner'], 'cyan')}")


def print_social_audit(items: list[dict[str, Any]]) -> None:
    """``social audit``：关系变更审计链。"""
    if not items:
        print(c("（审计为空）", "dim"))
        return
    for a in items:
        ts = _local_hms(a.get("ts", ""))
        scopes = f"  {_fmt_scopes(a.get('scopes'))}" if a.get("scopes") else ""
        note = f"  {c(a.get('note', ''), 'dim')}" if a.get("note") else ""
        decision = f"  {c(a.get('decision', ''), 'dim')}" if a.get("decision") else ""
        print(f"{c(ts, 'dim')}  {c(str(a.get('action', '?')).ljust(8), 'bold')} "
              f"{a.get('actor', '?')} {c('->', 'dim')} {a.get('peer', '?')}"
              f"{scopes}{note}{decision}")


def print_social_discover(items: list[dict[str, Any]], need: str = "") -> None:
    """``social discover``：按匹配度推荐的候选，**带打分明细**。

    明细不是装饰——自主交友必须能回答「为什么推荐它」，
    否则事后没法区分「策略太松」和「实现有 bug」。
    """
    if not items:
        hint = f"（没有与 `{need}` 匹配的候选）" if need else "（没有可推荐的候选）"
        print(c(hint, "dim"))
        return
    for it in items:
        pct = int(it.get("matchPercent") or 0)
        bar = "█" * max(1, pct // 10)
        print(f"{c('●', 'cyan')} {c(it['id'], 'bold')}  {it.get('name', '')}  "
              f"{c('[' + str(it.get('kind', '')) + ']', 'dim')}  "
              f"{c(f'{pct}%', 'green')} {c(bar, 'dim')}")
        if it.get("bio"):
            print(f"    {c(it['bio'], 'dim')}")
        b = it.get("breakdown") or {}
        suffix = c("  · 被引荐", "magenta") if it.get("referred") else ""
        print(f"    {c('技能互补', 'dim')} {b.get('skill', 0):.2f}"
              f"  {c('共同好友', 'dim')} {it.get('commonFriends', 0)}"
              f"  {c('距离', 'dim')} {it.get('distance', '?')}{suffix}")


def print_social_profile(data: dict[str, Any]) -> None:
    """``social profile``：别人眼里的某人——**只有共同好友数，没有名单**。"""
    m = data.get("member", {})
    print(f"{c(str(m.get('id', '?')), 'bold')}  {m.get('name', '')}  "
          f"{c('[' + str(m.get('kind', '')) + ']', 'dim')}")
    if m.get("bio"):
        print(f"    {c(m['bio'], 'dim')}")
    flags = []
    if data.get("isSelf"):
        flags.append("就是我自己")
    if data.get("isFriend"):
        flags.append("已是好友")
    if data.get("referred"):
        flags.append("被引荐过")
    print(f"    共同好友 {c(str(data.get('commonFriends', 0)), 'green')}"
          f" · 距离 {data.get('distance', '?')}"
          f" · 可见 {'是' if data.get('visible') else '否'}")
    if flags:
        print(f"    {c(' / '.join(flags), 'cyan')}")
    print(c("    （好友名单不对外暴露，只出数量）", "dim"))


def print_social_introductions(items: list[dict[str, Any]]) -> None:
    """``social intro``：别人引荐给我的人。"""
    if not items:
        print(c("（没有收到引荐）", "dim"))
        return
    for it in items:
        print(f"{c('●', 'magenta')} {c(str(it.get('peer', '?')), 'bold')}  "
              f"{it.get('peerName', '')}  {c('由 ' + str(it.get('introducer', '')), 'dim')}")
        if it.get("note"):
            print(f"    {c(it['note'], 'dim')}")


def print_social_approvals(items: list[dict[str, Any]]) -> None:
    """``social approvals``：待 owner 拍板的自主交友待办。

    每条都要能回答「为什么轮到我拍板」——所以 ``policy`` 与 ``detail``
    必须打出来，否则 owner 只能盲批（§6 约束 3）。
    """
    if not items:
        print(c("（没有待办）", "dim"))
        return
    for it in items:
        kind = it.get("kind", "")
        label = "别人申请我" if kind == "accept" else "我要申请别人"
        other = it.get("requester") if kind == "accept" else it.get("target")
        print(f"{c('●', 'yellow')} {c(str(it.get('id', '?')), 'bold')}  "
              f"{c(label, 'cyan')}  {c(str(other or '?'), 'bold')}")
        if it.get("need"):
            print(f"    需求：{it['need']}")
        if it.get("message"):
            print(f"    理由：{c(it['message'], 'dim')}")
        print(f"    {c('命中策略', 'dim')} {it.get('policy', '')}")
        if it.get("detail"):
            print(f"    {c(it['detail'], 'dim')}")
        if it.get("scopes"):
            print(f"    拟授予：{', '.join(it['scopes'])}")


def print_social_need(result: dict[str, Any]) -> None:
    """``social need``：报告能力缺口之后发生了什么。"""
    action = result.get("action", "")
    verdict = {
        "sent": ("已发起申请", "green"),
        "pending": ("已挂到 owner 待办，等人批", "yellow"),
        "skip": ("本轮跳过", "dim"),
        "silent": ("静默丢弃", "dim"),
        "ignore": ("未介入", "dim"),
    }
    text, colour = verdict.get(action, (action or "未知", "dim"))
    print(f"{c('需求', 'dim')} {result.get('need', '')}  →  {c(text, colour)}")
    if result.get("policy"):
        print(f"    {c('命中策略', 'dim')} {result['policy']}")
    if result.get("detail"):
        print(f"    {c(result['detail'], 'dim')}")
    cands = result.get("candidates") or []
    if cands:
        shown = "、".join(f"{x['id']}({int((x.get('affinity') or 0) * 100)}%)" for x in cands)
        print(f"    {c('候选', 'dim')} {shown}")
    if result.get("asked"):
        print(f"    {c('对象', 'dim')} {result['asked'].get('peer')}")


# --------------------------------------------------------------------------- #
# 进程内执行
# --------------------------------------------------------------------------- #


async def _inproc_ask(args: argparse.Namespace) -> int:
    from .models import Message
    from .registry import bootstrap

    reg = bootstrap(get_settings())
    await reg.check_health()

    if args.agent:
        if not reg.has(args.agent):
            print(c(f"未找到 agent `{args.agent}`", "red"), file=sys.stderr)
            return 2
        rec = reg.get(args.agent)
    else:
        rec = reg.route(args.prompt)
        if rec is None:
            print(c("没有可用 agent 可路由", "red"), file=sys.stderr)
            return 2
        print(c(f"自动路由 -> {rec.id} ({rec.name})", "dim"))

    # `ask` 就是「派活」，所以走 delegate 那道门（无 members.yaml 时全放行）
    from .relations import Scope

    graph = _inproc_graph(reg)
    actor = _inproc_actor(graph, getattr(args, "as_member", None))
    _warn_unknown_actor(graph, actor)
    if graph.enabled and not graph.delegable(actor, rec.id):
        data = graph.refusal_data(actor, rec.id, Scope.DELEGATE)
        print(c(f"社交门禁：`{actor}` 没有 `{rec.id}` 的执行授权（delegate）", "red"),
              file=sys.stderr)
        print(c(f"  {data.get('hint', '')}", "dim"), file=sys.stderr)
        return 2

    print(c(f"── 任务下发 ── agent={rec.id}", "magenta"))
    message = Message.user(args.prompt)
    task = reg.new_task(rec.id, message)

    async for event in reg.execute(rec, task, message):
        kind = event.kind
        if kind == "status-update":
            state = event.status.state.value
            icon = STATE_ICON.get(state, "·")
            if state in ("working", "submitted"):
                print(f"{c(icon, 'yellow')} {state}")
            else:
                print(f"{c(icon, STATE_COLOR.get('healthy' if state == 'completed' else 'red', 'dim'))} {state}")
        elif kind == "artifact-update":
            if event.artifact.name == "output":
                text = event.artifact.parts[-1].text if event.artifact.parts else ""
                if text:
                    sys.stdout.write(text)
                    sys.stdout.flush()

    print()
    print(c("── 结束 ──", "magenta"))
    print(c(f"taskId={task.id}  state={task.status.state.value}", "dim"))
    return 0 if task.status.state.value == "completed" else 1


async def _inproc_collab(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator
    from .registry import bootstrap

    reg = bootstrap(get_settings())
    await reg.check_health()
    # 协同 = 让 agent 干活，所以要过 delegate 门禁（无 members.yaml 时全放行）
    graph = _inproc_graph(reg)
    actor = _inproc_actor(graph, getattr(args, "as_member", None))
    _warn_unknown_actor(graph, actor)
    orch = Orchestrator(reg, reg.bus, guard=graph)

    options: dict[str, Any] = {}
    if args.synthesizer:
        options["synthesizer"] = args.synthesizer
    if args.reviewer:
        options["reviewer"] = args.reviewer
    if args.rounds:
        options["rounds"] = args.rounds
    if args.top_k:
        options["topK"] = args.top_k

    agent_ids = [a.strip() for a in args.agents.split(",")] if args.agents else []
    run = orch.create_run(args.mode, args.prompt, agent_ids, options, actor=actor)
    print(c(f"── 协同 {args.mode} · run={run.id} ──", "magenta"))

    async def watch() -> None:
        async for ev in orch.stream(run.id):
            etype = ev.get("event")
            if etype == "collab-snapshot":
                continue
            if etype == "plan":
                plan = ev.get("plan", {})
                items = plan.get("steps") or plan.get("stages") or []
                print(c("计划: ", "dim") + " -> ".join(str(i.get("agent")) for i in items))
            elif etype == "step-started":
                s = ev["step"]
                print(f"{c('◐', 'yellow')} [{s['label']}] {s['agentName'] or s['agentId']} 开始")
            elif etype == "step-finished":
                s = ev["step"]
                mark = c("●", "green") if s["state"] == "completed" else c("✗", "red")
                print(f"{mark} [{s['label']}] 完成（{s['durationMs']}ms, {len(s['output'])} 字）")
                if s.get("error"):
                    print(c(f"    {s['error']}", "red"))
            elif etype == "round-finished":
                print(c(f"  ── 第 {ev.get('round')} 轮结束 ──", "dim"))
            elif etype == "collab-finished":
                pass

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 watcher 先挂上订阅，避免漏掉开头的事件
    from .relations import SocialNotPermitted

    try:
        await orch.run_sync(run)
    except SocialNotPermitted as exc:
        watcher.cancel()
        print(c(f"社交门禁拒绝：{exc}", "red"), file=sys.stderr)
        if run.refusedAgents:
            print(c(f"  缺 `delegate` 的 agent：{'、'.join(run.refusedAgents)}", "yellow"),
                  file=sys.stderr)
            print(c("  让对方向你授予 delegate：a2a-hub social grant <你> --scopes chat,delegate",
                    "dim"), file=sys.stderr)
        return 2
    await asyncio.wait_for(watcher, timeout=5)

    print()
    print(c("══ 协同结果 ══", "bold"))
    print(run.result or c("(无产出)", "dim"))
    print()
    print(c(f"状态: {run.status} · 步骤: {len(run.steps)}", "dim"))
    return 0 if run.status == "completed" else 1


def _fix_positional(args: argparse.Namespace, sub: str) -> None:
    """把落进 ``agent`` 槽里的消息文本挪回 ``text``。

    argparse 只给了两个位置参数槽（agent / text）：``im chat <agent> "文本"``
    正好用满，但 ``im group --members a,b "文本"`` 和 ``im say -c CONV "文本"``
    的正文会落进 ``agent`` 槽。这里统一归一化，免得调用方得记住几个子命令的
    位置参数含义各不相同。
    """
    if sub in ("group", "say") and args.agent is not None and args.text is None:
        args.text, args.agent = args.agent, None


async def _inproc_im(args: argparse.Namespace) -> int:
    """会话层的进程内执行。

    注意：进程内模式下会话只活在本次命令里。所以 ``chat`` / ``group`` 这类
    「建会话 + 发消息 + 等回复 + 打印」一步到位的用法才是主角——
    想连续对话请起服务再用 ``--url`` 连过去。
    """
    from .registry import bootstrap
    from .social import SocialHub

    reg = bootstrap(get_settings())
    # 会话层同样要过门禁：否则本地 CLI 会变成绕过「加好友才能交流」的后门。
    # 没 members.yaml 时 graph.enabled=False，一切照旧。
    graph = _inproc_graph(reg)
    actor = _inproc_actor(graph, getattr(args, "as_member", None))
    _warn_unknown_actor(graph, actor)
    hub = SocialHub(reg, reg.bus, guard=graph)
    sub = args.im_cmd
    _fix_positional(args, sub)

    # 通讯录与「群里自动挑人接话」都依赖健康状态。不先探一次的话，
    # health 还是 unknown，自动路由就会挑中一个根本跑不起来的 agent。
    # （走 registry 的 TTL 缓存，重复调用不会真的重复打网络。）
    if sub != "log":
        await reg.check_health(force=bool(args.refresh))

    def _members() -> list[str]:
        return [m.strip() for m in (args.members or "").split(",") if m.strip()]

    if sub == "contacts":
        print_im_contacts(hub.contacts(actor))
        return 0

    if sub == "open":
        if not reg.has(args.agent):
            print(c(f"未找到 agent `{args.agent}`", "red"), file=sys.stderr)
            return 2
        conv = hub.open_direct(args.agent, owner=actor)
        print(hub.summary(conv, actor)["id"])
        return 0

    if sub == "group":
        members = _members()
        if not members:
            print(c("建群需要 --members，例如 --members echo,static", "red"), file=sys.stderr)
            return 2
        try:
            conv = hub.create_group(members, args.title or "", actor=actor)
        except Exception as exc:  # noqa: BLE001
            print(c(f"建群失败：{exc}", "red"), file=sys.stderr)
            return 2
        if not args.text:
            print(hub.summary(conv, actor)["id"])
            return 0
        return await _im_converse(hub, conv, args, actor)

    if sub == "say":
        if not args.conversation:
            print(c("发消息需要 -c/--conversation", "red"), file=sys.stderr)
            return 2
        try:
            res = await hub.send(args.conversation, args.text or "", sender=actor)
        except Exception as exc:  # noqa: BLE001
            print(c(f"发送失败：{exc}", "red"), file=sys.stderr)
            return 2
        woke = res["woke"]
        print(c(f"已发送 · 唤醒 {', '.join(woke) if woke else '（无人，仅存档）'}", "dim"))
        if res.get("refused"):
            print(c(f"被门禁挡下：{'、'.join(res['refused'])}", "yellow"))
        if args.wait and woke:
            await hub.wait_idle(args.conversation, timeout=args.timeout)
            print_im_transcript(**hub.history(args.conversation, limit=args.limit, viewer=actor))
        return 0

    if sub == "log":
        if not args.conversation:
            print(c("查看记录需要 -c/--conversation", "red"), file=sys.stderr)
            return 2
        try:
            print_im_transcript(
                **hub.history(args.conversation, limit=args.limit, viewer=actor)
            )
        except KeyError as exc:
            print(c(str(exc), "red"), file=sys.stderr)
            return 2
        return 0

    if sub == "chat":
        if not args.agent or not args.text:
            print(c("用法：im chat <agent> \"消息内容\"", "red"), file=sys.stderr)
            return 2
        if not reg.has(args.agent):
            print(c(f"未找到 agent `{args.agent}`", "red"), file=sys.stderr)
            return 2
        return await _im_converse(hub, hub.open_direct(args.agent, owner=actor), args, actor)

    print(c(f"未知子命令 `{sub}`", "red"), file=sys.stderr)
    return 2


async def _im_converse(
    hub: Any, conv: Any, args: argparse.Namespace, actor: str = "user"
) -> int:
    """建好会话后：发一条消息 → 等所有 agent 回完 → 打印整段对话。"""
    res = await hub.send(conv.id, args.text or "", sender=actor)
    woke = res["woke"]
    if not woke:
        print_im_transcript(**hub.history(conv.id, limit=args.limit, viewer=actor))
        if res.get("refused"):
            print(c(
                f"没有人被唤醒——{ '、'.join(res['refused']) } 被门禁挡下"
                "（多半是还没加好友，`social add` 一下）。",
                "yellow",
            ))
        else:
            print(c("没有人被唤醒——群里没人被 @，自动路由也没命中。", "yellow"))
        return 1
    print(c(f"已发送，等待 {len(woke)} 位回复：{', '.join(woke)}", "dim"))
    await hub.wait_idle(conv.id, timeout=args.timeout)
    print_im_transcript(**hub.history(conv.id, limit=args.limit, viewer=actor))
    failed = [d for d in hub.get(conv.id).deliveries if not d.is_terminal or d.error]
    return 1 if failed else 0


def _inproc_graph(reg: Any = None) -> Any:
    """进程内构造关系图。

    与 ``server.Hub._build_graph`` 保持同一套装配逻辑：读 members.yaml →
    按 mode 建图 → 把 registry 里的 agent 补成节点（缺声明时 owner 留空）。
    不补节点的话，通讯录里会看不到那些没写进 members.yaml 的 agent。

    ``reg`` 可传入已建好的注册中心（``bootstrap`` 幂等，传不传都拿到同一个）。
    """
    from .relations import SocialGraph

    s = get_settings()
    graph = SocialGraph(
        SocialGraph.load_members(s.members_path()),
        mode=s.social_mode,
        path=s.relations_path(),
    )
    if graph.enabled:
        from .registry import bootstrap

        r = reg or bootstrap(s)
        graph.ensure_agents((rec.id, rec.name) for rec in r.list_records())

        def caps(member_id: str) -> dict:
            """把 registry 的能力喂给关系层（走注入，不让关系层 import registry）。"""
            from .relations import SocialGraph

            agent_id = SocialGraph.agent_id_of(member_id)
            if agent_id and r.has(agent_id):
                rec = r.get(agent_id)
                return {
                    "name": rec.name,
                    "bio": rec.description,
                    "description": rec.description,
                    "tags": list(rec.spec.tags),
                    "skills": [
                        f"{sk.name} {sk.description} {' '.join(sk.tags)}"
                        for sk in rec.adapter.skills
                    ],
                }
            m = graph.get_member(member_id)
            return {"bio": m.bio if m else ""}

        graph.set_capability_resolver(caps)
        # 社交简报：与 server.Hub 同一套装配——进程内 CLI 派活也让 agent
        # 收到「你是谁/好友/信号协议」。社交层关闭时不注入。
        if s.social_briefing:
            from .relations import social_briefing

            r2 = reg or bootstrap(s)
            r2.set_briefing_resolver(
                lambda agent_id: social_briefing(graph, agent_id)
            )
    return graph


def _social_init(args: argparse.Namespace) -> int:
    """``social init`` —— 一键启用社交层。

    只负责「把 members.yaml 生出来」。进程内 CLI 每次都是新进程，所以生成完
    下一次调用自然就启用了；HTTP 服务侧则由 ``Hub.ensure_social`` 就地热启用，
    两边都不需要用户重启任何东西。

    幂等：文件已存在就原样返回，**绝不覆盖**用户手改过的配置。
    """
    from .social_boot import bootstrap_members

    s = get_settings()
    info = bootstrap_members(
        members_path=s.members_path(), agents_path=s.agents_path()
    )
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    path = info.get("path")
    if info.get("created"):
        print(c(f"已生成 {path}", "green"))
        print(f"  {info.get('reason')}")
        print()
        print("社交门禁已启用。接下来：")
        print("  python run.py social me           # 看看自己的名片")
        print("  python run.py social find <关键词>  # 找人")
        print()
        print(c("说明：", "yellow") + "生成的配置不写任何 token（本地开发模式，不会把自己"
              "锁在门外）；要从外部访问再加 tokens。删除该文件即可关闭门禁。")
        return 0

    print(c(f"{path} 已存在，未做任何改动。", "yellow"))
    print(f"  {info.get('reason')}")
    print("  想看完整能力模板：config/members.example.yaml")
    return 0


def _inproc_actor(graph: Any, explicit: Optional[str] = None) -> str:
    """进程内模式的操作者。

    进程内没有 token，所以身份只能靠 ``--as`` 显式指定；不指定时用
    「本机的默认人类成员」。门禁没启用时退回保留字 ``USER``，
    与 v0.3.0 的历史记录保持一致（那时候所有消息的 sender 都是 ``user``）。

    多人类部署下 ``default_member_id`` 是 ``human:default``——
    一个**没被任何人声明过**的身份。这是刻意的（宁可不猜），
    但那也意味着本地 CLI 在这种部署里必须用 ``--as`` 说明自己是谁。
    """
    if explicit:
        return graph.normalize(explicit) if graph.enabled else explicit
    return graph.default_member_id if graph.enabled else "user"


def _warn_unknown_actor(graph: Any, actor: str) -> None:
    """``--as`` 打错字时给一句提醒。

    不报错是因为关系图本来就不要求节点先声明（否则新成员永远加不进来），
    但一个合成身份**零权限**，不说清楚会让人以为门禁坏了。
    """
    if graph.enabled and graph.get_member(actor) is None:
        print(c(f"提示：`{actor}` 未在 members.yaml 里声明，它会以「零权限的陌生成员」身份操作。",
                "yellow"), file=sys.stderr)


async def _inproc_social(args: argparse.Namespace) -> int:
    """社交层的进程内执行。

    进程内模式没有 token，调用者就是「本机的默认人类成员」——
    ``--as`` 可以换成别的成员（本地排查多租户配置时很好用）。
    """
    from .relations import RelationState, SocialError

    sub = args.social_cmd
    # init 要在建图之前处理：建图时门禁还是关的，先生成配置再建图才拿得到
    # 启用后的状态（进程内每次调用都是新进程，所以生成完重新读一次即可）。
    if sub == "init":
        return _social_init(args)

    graph = _inproc_graph()
    target = args.target
    # 进程内没有 token：默认以「本机人类成员」身份操作。``--as`` 可切换成别的成员
    # （多租户配置排查时很有用）；对 accept/reject 而言 ``--as`` 表示
    # 「我代表自己的 agent 表态」，此时仍以 caller 身份做归属校验。
    caller = graph.default_member_id
    # `--as` 先归一化：否则 `--as seafish` 会被当成 agent 而不是人类，
    # 门禁的归属校验就会失败得莫名其妙。
    actor = graph.normalize(args.as_member) if args.as_member else caller

    def need_enabled() -> bool:
        if graph.enabled:
            return True
        print(
            c(
                "社交层未启用：未找到 members.yaml（或 A2A_SOCIAL_MODE=off）。"
                f"\n当前配置路径 {get_settings().members_file}",
                "red",
            ),
            file=sys.stderr,
        )
        return False

    def scopes_of(raw: Optional[str]) -> Optional[list[str]]:
        if raw is None:
            return None
        items = [x.strip() for x in raw.replace(" ", "").split(",") if x.strip()]
        return items

    def guard(fn: Any) -> Optional[Any]:
        """统一把 ``SocialError`` 转成一行红字 + 退出码 2。"""
        try:
            return fn()
        except SocialError as exc:
            print(c(f"{exc}", "red"), file=sys.stderr)
            return None

    # ---- 只读命令 ---------------------------------------------------------- #
    if sub == "me":
        if not need_enabled():
            return 2
        data = graph.me(actor)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print_social_me(data)
        return 0

    if sub == "find":
        if not need_enabled():
            return 2
        items = graph.search(target or "", limit=args.limit)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print_social_members(items, target or "")
        return 0

    if sub == "friends":
        if not need_enabled():
            return 2
        items = graph.relations_of(actor, states={RelationState.FRIEND})
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(c(f"── 好友（{len(items)}）──", "bold"))
            for it in items:
                print_social_relation(it, indent=2)
            if not items:
                print(c("   （还没有好友，试试 `social add <agent> --reason ...`）", "dim"))
        return 0

    if sub == "profile":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social profile <成员>", "red"), file=sys.stderr)
            return 2
        data = graph.profile(target, actor)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print_social_profile(data)
        return 0

    if sub == "discover":
        if not need_enabled():
            return 2
        items = graph.discover(actor, need=args.need or "", limit=args.limit)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(c(f"── 发现（{len(items)}）──", "bold"))
            print_social_discover(items, args.need or "")
        return 0

    if sub in ("introductions", "intro"):
        if not need_enabled():
            return 2
        items = graph.introductions(actor)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(c(f"── 别人引荐给我的（{len(items)}）──", "bold"))
            print_social_introductions(items)
        return 0

    # ---- 自主交友（§6） --------------------------------------------------- #
    if sub == "need":
        if not need_enabled():
            return 2
        # 缺口可以从 `--need` 或位置参数给：`social need "OCR 表格提取"`
        need_text = (args.need or target or "").strip()
        if not need_text:
            print(
                c('need 需要一个能力缺口，例如：social need "OCR 表格提取"', "red"),
                file=sys.stderr,
            )
            return 2
        result = graph.request_for_need(actor, need_text, reason=args.reason or "")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_social_need(result)
        return 0

    if sub == "approvals":
        if not need_enabled():
            return 2
        items = graph.pending_approvals(owner=actor)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(c(f"── 待我拍板的自主交友（{len(items)}）──", "bold"))
            print_social_approvals(items)
            if items:
                print(c("   批准：social approve <id>   驳回：social deny <id> --reason …", "dim"))
        return 0

    if sub in ("approve", "deny"):
        if not need_enabled():
            return 2
        pid = target
        if not pid:
            print(c(f"{sub} 需要一个待办 id（social approvals 里查看）", "red"), file=sys.stderr)
            return 2
        if sub == "approve":
            result = guard(lambda: graph.approve_pending(actor, pid, scopes_of(args.scopes)))
        else:
            result = guard(lambda: graph.deny_pending(actor, pid, args.reason or ""))
        if result is None:
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            state = (result.get("approval") or {}).get("state", "")
            mark = c("✓", "green") if state == "approved" else c("✗", "yellow")
            print(f"{mark} {pid} → {state}")
            rel = result.get("relation")
            if rel:
                pair = rel.get("pair") or []
                peer = next((p for p in pair if graph.normalize(p) != actor), "")
                print(f"    与 {c(str(peer), 'bold')} 的关系 → {rel.get('state', '')}")
        return 0

    if sub == "relations":
        if not need_enabled():
            return 2
        states = None
        if args.state:
            try:
                states = {RelationState(args.state)}
            except ValueError:
                print(c(f"未知状态 `{args.state}`", "red"), file=sys.stderr)
                return 2
        items = graph.relations_of(actor, states=states)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(c(f"── 我的关系（{len(items)}）──", "bold"))
            for it in items:
                print_social_relation(it, indent=2)
            if not items:
                print(c("   （没有任何关系记录）", "dim"))
        return 0

    if sub in ("inbox", "outbox", "pending"):
        if not need_enabled():
            return 2
        boxes: list[tuple[str, list[dict[str, Any]]]] = []
        if sub in ("inbox", "pending"):
            boxes.append(("待我处理", graph.inbox(actor)))
        if sub in ("outbox", "pending"):
            boxes.append(("我发出的在途申请", graph.outbox(actor)))
        if args.json:
            print(json.dumps({k: v for k, v in boxes}, ensure_ascii=False, indent=2))
        else:
            for title, items in boxes:
                print(c(f"── {title}（{len(items)}）──", "bold"))
                for it in items:
                    print_social_relation(it, indent=2)
                if not items:
                    print(c("   （空）", "dim"))
        return 0

    if sub == "audit":
        if not need_enabled():
            return 2
        items = graph.trail(args.peer, limit=args.limit)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print_social_audit(items)
        return 0

    # ---- 写命令 ------------------------------------------------------------ #
    if sub == "add":
        if not need_enabled():
            return 2
        if not target:
            print(c('用法：social add <agent> --reason "为什么想加"', "red"), file=sys.stderr)
            return 2
        reason = args.reason
        if not reason:
            # 理由必填是刻意的防骚扰设计，与其等后端报错不如在前端就拦住
            print(c('申请必须带 --reason "为什么想加"（防骚扰）', "red"), file=sys.stderr)
            return 2
        rel = guard(lambda: graph.request(actor, target, reason, scopes_of(args.scopes)))
        if rel is None:
            return 2
        print(c(f"已向 {graph.normalize(target)} 发出好友申请", "green"))
        print_social_relation(graph.describe(rel, actor), indent=2)
        return 0

    if sub == "introduce":
        if not need_enabled():
            return 2
        if not target or not args.to:
            print(c("用法：social introduce <peer> --to <成员> [--note 推荐语]", "red"),
                  file=sys.stderr)
            return 2
        res = guard(lambda: graph.introduce(actor, args.to, target, args.note or ""))
        if res is None:
            return 2
        print(c(f"已把 {graph.normalize(target)} 引荐给 {graph.normalize(args.to)}"
                "（引荐本身不授予任何权限）", "green"))
        print_social_profile(res)
        return 0

    if sub == "accept":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social accept <peer> [--grant peek,chat]", "red"), file=sys.stderr)
            return 2
        rel = guard(
            lambda: graph.accept(caller, target, scopes_of(args.scopes), as_member=args.as_member)
        )
        if rel is None:
            return 2
        print(c(f"已与 {graph.normalize(target)} 成为好友", "green"))
        print_social_relation(graph.describe(rel, actor), indent=2)
        return 0

    if sub == "reject":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social reject <peer> [--reason ...]", "red"), file=sys.stderr)
            return 2
        rel = guard(
            lambda: graph.reject(caller, target, args.reason or "", as_member=args.as_member)
        )
        if rel is None:
            return 2
        # 拒绝理由只进自己的审计，不告诉对方——措辞上要跟用户说清楚
        print(c(f"已拒绝 {graph.normalize(target)} 的申请（理由只记在你自己的审计里）", "dim"))
        return 0

    if sub == "cancel":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social cancel <peer>", "red"), file=sys.stderr)
            return 2
        if guard(lambda: graph.cancel(actor, target)) is None:
            return 2
        print(c(f"已撤回发给 {graph.normalize(target)} 的申请", "dim"))
        return 0

    if sub == "grant":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social grant <peer> --scopes peek,chat,delegate", "red"), file=sys.stderr)
            return 2
        rel = guard(lambda: graph.set_grant(actor, target, scopes_of(args.scopes) or []))
        if rel is None:
            return 2
        print(c(f"已更新对 {graph.normalize(target)} 的权限", "green"))
        print_social_relation(graph.describe(rel, actor), indent=2)
        return 0

    if sub == "revoke":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social revoke <peer>", "red"), file=sys.stderr)
            return 2
        if guard(lambda: graph.revoke(actor, target)) is None:
            return 2
        print(c(f"已解除与 {graph.normalize(target)} 的好友关系（历史记录保留）", "dim"))
        return 0

    if sub == "block":
        if not need_enabled():
            return 2
        if not target:
            print(c("用法：social block <peer> [--undo]", "red"), file=sys.stderr)
            return 2
        if args.undo:
            if guard(lambda: graph.unblock(actor, target)) is None:
                return 2
            print(c(f"已解除对 {graph.normalize(target)} 的拉黑", "dim"))
        else:
            if guard(lambda: graph.block(actor, target)) is None:
                return 2
            print(c(f"已拉黑 {graph.normalize(target)}（只有你能解除）", "yellow"))
        return 0

    print(c(f"未知子命令 `{sub}`", "red"), file=sys.stderr)
    return 2


async def _inproc_main(args: argparse.Namespace) -> int:
    from .registry import bootstrap

    reg = bootstrap(get_settings())

    if args.cmd == "agents":
        await reg.check_health(force=args.refresh)
        snap = reg.snapshot()
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print_agents(snap)
        return 0

    if args.cmd == "health":
        res = await reg.check_health(force=True)
        for aid, h in res.items():
            print(f"{c('●', STATE_COLOR.get(h.get('status', 'unknown'), 'dim'))} {aid}: "
                  f"{h.get('status')} {c(h.get('detail', ''), 'dim')}")
        return 0

    if args.cmd == "card":
        card = reg.agent_card(args.agent) if args.agent else reg.hub_card()
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "modes":
        print(json.dumps(
            {
                "delegate": "委派 —— 按能力自动路由到最合适的单个 agent（可选 reviewer 评审）",
                "broadcast": "广播 —— 多 agent 并行作答，再由 synthesizer 收敛成一份结论",
                "pipeline": "流水线 —— 串行接力，上游产出作为下游输入",
                "roundtable": "圆桌 —— 多轮讨论，各自看到他人观点后修订，最终收敛",
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if args.cmd == "ask":
        return await _inproc_ask(args)

    if args.cmd == "collab":
        return await _inproc_collab(args)

    if args.cmd == "im":
        return await _inproc_im(args)

    if args.cmd == "social":
        return await _inproc_social(args)

    return 0


# --------------------------------------------------------------------------- #
# 远端 HTTP 执行
# --------------------------------------------------------------------------- #


async def _remote_main(args: argparse.Namespace) -> int:
    import httpx

    base = args.url.rstrip("/")
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    async with httpx.AsyncClient(timeout=600, headers=headers) as client:
        if args.cmd == "agents":
            r = await client.get(f"{base}/agents", params={"refresh": args.refresh})
            r.raise_for_status()
            data = r.json()
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print_agents(data["agents"])
            return 0

        if args.cmd == "health":
            r = await client.post(
                f"{base}/", json={"jsonrpc": "2.0", "id": 1, "method": "agents/health", "params": {}}
            )
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "card":
            path = (
                f"{base}/agents/{args.agent}/.well-known/agent.json"
                if args.agent
                else f"{base}/.well-known/agent.json"
            )
            r = await client.get(path)
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "modes":
            r = await client.post(
                f"{base}/", json={"jsonrpc": "2.0", "id": 1, "method": "collab/modes", "params": {}}
            )
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "ask":
            rpc = {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/stream",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": args.prompt}]}},
            }
            if args.agent:
                rpc["params"]["agentId"] = args.agent
            async with client.stream("POST", f"{base}/", json=rpc) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if payload.get("kind") == "artifact-update":
                            art = payload.get("artifact") or {}
                            if art.get("name") == "output" and art.get("parts"):
                                sys.stdout.write(art["parts"][-1].get("text", ""))
                                sys.stdout.flush()
            print()
            return 0

        if args.cmd == "collab":
            options: dict[str, Any] = {}
            if args.synthesizer:
                options["synthesizer"] = args.synthesizer
            if args.reviewer:
                options["reviewer"] = args.reviewer
            if args.rounds:
                options["rounds"] = args.rounds
            if args.top_k:
                options["topK"] = args.top_k
            body = {
                "mode": args.mode,
                "prompt": args.prompt,
                "options": options,
                "blocking": False,
                "agentIds": [a.strip() for a in args.agents.split(",")] if args.agents else [],
            }
            r = await client.post(f"{base}/collab", json=body)
            r.raise_for_status()
            run_id = r.json()["id"]
            print(c(f"── 协同 {args.mode} · run={run_id} ──", "magenta"))
            async with client.stream("GET", f"{base}/collab/{run_id}/events") as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("event")
                    if etype == "step-started":
                        s = ev["step"]
                        print(f"{c('◐', 'yellow')} [{s['label']}] {s['agentName']} 开始")
                    elif etype == "step-finished":
                        s = ev["step"]
                        mark = c("●", "green") if s["state"] == "completed" else c("✗", "red")
                        print(f"{mark} [{s['label']}] 完成（{s['durationMs']}ms, {len(s['output'])} 字）")
                    elif etype == "collab-finished":
                        break
            detail = await client.get(f"{base}/collab/{run_id}")
            run = detail.json()
            print()
            print(c("══ 协同结果 ══", "bold"))
            print(run.get("result") or "(无产出)")
            return 0 if run.get("status") == "completed" else 1

        if args.cmd == "im":
            return await _remote_im(client, base, args)

        if args.cmd == "social":
            return await _remote_social(client, base, args)

    return 0


# --------------------------------------------------------------------------- #
# 会话层的远端执行（CLI 只当客户端，会话状态全在服务端）
# --------------------------------------------------------------------------- #


async def _remote_im(client: Any, base: str, args: argparse.Namespace) -> int:
    sub = args.im_cmd
    _fix_positional(args, sub)

    async def fail(resp: Any, what: str) -> int:
        print(c(f"{what}失败：HTTP {resp.status_code} {resp.text[:300]}", "red"), file=sys.stderr)
        return 2

    if sub == "contacts":
        if args.refresh:
            await client.post(
                f"{base}/",
                json={"jsonrpc": "2.0", "id": 1, "method": "agents/health", "params": {"force": True}},
            )
        params = {"as": args.as_member} if args.as_member else None
        r = await client.get(f"{base}/im/contacts", params=params)
        if r.status_code != 200:
            return await fail(r, "获取通讯录")
        print_im_contacts(r.json()["contacts"])
        return 0

    if sub == "open":
        r = await client.post(f"{base}/im/conversations", json={"kind": "direct", "agent": args.agent})
        if r.status_code != 200:
            return await fail(r, "打开单聊")
        print(r.json()["id"])
        return 0

    if sub == "group":
        members = [m.strip() for m in (args.members or "").split(",") if m.strip()]
        if not members:
            print(c("建群需要 --members，例如 --members echo,static", "red"), file=sys.stderr)
            return 2
        r = await client.post(
            f"{base}/im/conversations",
            json={"kind": "group", "members": members, "title": args.title or ""},
        )
        if r.status_code != 200:
            return await fail(r, "建群")
        conv_id = r.json()["id"]
        if not args.text:
            print(conv_id)
            return 0
        return await _remote_converse(client, base, conv_id, args)

    if sub == "say":
        if not args.conversation:
            print(c("发消息需要 -c/--conversation", "red"), file=sys.stderr)
            return 2
        r = await client.post(
            f"{base}/im/conversations/{args.conversation}/messages", json={"text": args.text or ""}
        )
        if r.status_code != 200:
            return await fail(r, "发送")
        woke = r.json().get("woke") or []
        print(c(f"已发送 · 唤醒 {', '.join(woke) if woke else '（无人，仅存档）'}", "dim"))
        if args.wait and woke:
            await _remote_wait(client, base, args.conversation, args)
        return 0

    if sub == "log":
        if not args.conversation:
            print(c("查看记录需要 -c/--conversation", "red"), file=sys.stderr)
            return 2
        r = await client.get(
            f"{base}/im/conversations/{args.conversation}", params={"limit": args.limit}
        )
        if r.status_code != 200:
            return await fail(r, "拉取记录")
        print_im_transcript(**r.json())
        return 0

    if sub == "chat":
        if not args.agent or not args.text:
            print(c('用法：im chat <agent> "消息内容"', "red"), file=sys.stderr)
            return 2
        r = await client.post(f"{base}/im/conversations", json={"kind": "direct", "agent": args.agent})
        if r.status_code != 200:
            return await fail(r, "打开单聊")
        return await _remote_converse(client, base, r.json()["id"], args)

    print(c(f"未知子命令 `{sub}`", "red"), file=sys.stderr)
    return 2


async def _remote_converse(client: Any, base: str, conv_id: str, args: argparse.Namespace) -> int:
    """远端版「发消息 → 靠 SSE 等回复 → 打印对话」。"""
    r = await client.post(f"{base}/im/conversations/{conv_id}/messages", json={"text": args.text or ""})
    if r.status_code != 200:
        print(c(f"发送失败：HTTP {r.status_code} {r.text[:300]}", "red"), file=sys.stderr)
        return 2

    payload = r.json()
    woke = payload.get("woke") or []
    if not woke:
        h = (await client.get(f"{base}/im/conversations/{conv_id}")).json()
        print_im_transcript(**h)
        print(c("没有人被唤醒——群里没人被 @，自动路由也没命中。", "yellow"))
        return 1

    print(c(f"已发送，等待 {len(woke)} 位回复：{', '.join(woke)}", "dim"))
    pending = {d["id"] for d in payload.get("deliveries", [])}
    deadline = time.monotonic() + args.timeout
    try:
        async with client.stream("GET", f"{base}/im/conversations/{conv_id}/events") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event")
                if kind == "snapshot":
                    for d in ev.get("deliveries", []):
                        if d["id"] in pending and d["state"] in ("replied", "failed"):
                            pending.discard(d["id"])
                elif kind == "delivery":
                    d = ev["delivery"]
                    icon, color = DELIVERY_STYLE.get(d["state"], ("·", "dim"))
                    print(f"  {c(icon, color)} {d['agentName']} {c(d['state'], color)}")
                    if d["id"] in pending and d["state"] in ("replied", "failed"):
                        pending.discard(d["id"])
                if not pending or time.monotonic() > deadline:
                    break
    except KeyboardInterrupt:
        print(c("\n已中断等待，下面显示当前进度。", "dim"))

    h = (await client.get(f"{base}/im/conversations/{conv_id}")).json()
    print_im_transcript(**h)
    return 1 if any(d["state"] == "failed" for d in h.get("deliveries", [])) else 0


async def _remote_wait(client: Any, base: str, conv_id: str, args: argparse.Namespace) -> None:
    """等待远端会话把所有在途投递跑完，然后打印记录。"""
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        h = (await client.get(f"{base}/im/conversations/{conv_id}")).json()
        if all(d["state"] in ("replied", "failed") for d in h.get("deliveries", [])):
            print_im_transcript(**h)
            return
        await asyncio.sleep(0.25)
    h = (await client.get(f"{base}/im/conversations/{conv_id}")).json()
    print_im_transcript(**h)


# --------------------------------------------------------------------------- #
# 社交层的远端执行（关系状态全在服务端，CLI 只当客户端）
# --------------------------------------------------------------------------- #


async def _remote_social(client: Any, base: str, args: argparse.Namespace) -> int:
    sub = args.social_cmd
    target = args.target

    scopes: Optional[list[str]] = None
    if args.scopes is not None:
        scopes = [x.strip() for x in args.scopes.replace(" ", "").split(",") if x.strip()]

    def with_as(payload: dict[str, Any]) -> dict[str, Any]:
        """``--as`` 只在服务端认可「owner 代表自己的 agent」时才有意义。"""
        if args.as_member:
            payload["as"] = args.as_member
        return payload

    def q(extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """只读接口的 query：``--as`` 走 ``?as=``（服务端会校验归属）。"""
        params = dict(extra or {})
        if args.as_member:
            params["as"] = args.as_member
        return params

    async def fail(resp: Any, what: str) -> int:
        print(c(f"{what}失败：HTTP {resp.status_code} {resp.text[:300]}", "red"), file=sys.stderr)
        return 2

    async def show(resp: Any, what: str, render: Any) -> int:
        if resp.status_code != 200:
            return await fail(resp, what)
        data = resp.json()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            render(data)
        return 0

    def render_relations(data: dict[str, Any]) -> None:
        items = data.get("relations") or []
        print(c(f"── 我的关系（{len(items)}）──", "bold"))
        for it in items:
            print_social_relation(it, indent=2)
        if not items:
            print(c("   （没有任何关系记录）", "dim"))

    if sub == "me":
        return await show(
            await client.get(f"{base}/social/me", params=q()), "获取社交状态", print_social_me
        )

    if sub == "find":
        r = await client.get(
            f"{base}/social/members", params=q({"q": target or "", "limit": args.limit})
        )
        return await show(
            r, "搜索成员", lambda d: print_social_members(d.get("members") or [], target or "")
        )

    if sub == "profile":
        if not target:
            print(c("用法：social profile <成员>", "red"), file=sys.stderr)
            return 2
        return await show(
            await client.get(f"{base}/social/members/{target}"),
            "获取名片",
            print_social_profile,
        )

    if sub == "discover":
        r = await client.get(
            f"{base}/social/discover",
            params=q({"need": args.need or "", "limit": args.limit}),
        )
        return await show(
            r,
            "发现候选",
            lambda d: print_social_discover(d.get("candidates") or [], d.get("need") or ""),
        )

    if sub in ("introductions", "intro"):
        r = await client.get(f"{base}/social/introductions", params=q())
        return await show(
            r, "获取引荐", lambda d: print_social_introductions(d.get("introductions") or [])
        )

    if sub == "introduce":
        if not target or not args.to:
            print(c("用法：social introduce <peer> --to <成员> [--note 推荐语]", "red"),
                  file=sys.stderr)
            return 2
        r = await client.post(
            f"{base}/social/introductions",
            json={"peer": target, "to": args.to, "note": args.note or ""},
        )
        if r.status_code != 200:
            return await fail(r, "引荐")
        print(c(f"已把 {target} 引荐给 {args.to}（引荐本身不授予任何权限）", "green"))
        if args.json:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print_social_profile(r.json())
        return 0

    if sub == "need":
        need_text = (args.need or target or "").strip()
        if not need_text:
            print(c('用法：social need "OCR 表格提取" [--reason …]', "red"), file=sys.stderr)
            return 2
        body: dict[str, Any] = {"need": need_text, "reason": args.reason or ""}
        if args.as_member:
            body["as"] = args.as_member
        r = await client.post(f"{base}/social/need", json=body)
        if r.status_code != 200:
            return await fail(r, "报告能力缺口")
        data = r.json()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print_social_need(data)
        return 0

    if sub == "approvals":
        r = await client.get(f"{base}/social/pending", params=q())
        return await show(
            r, "获取待办", lambda d: print_social_approvals(d.get("pending") or [])
        )

    if sub in ("approve", "deny"):
        if not target:
            print(c(f"用法：social {sub} <待办 id>", "red"), file=sys.stderr)
            return 2
        path = "approve" if sub == "approve" else "deny"
        body = {"id": target}
        if sub == "approve":
            if scopes:
                body["scopes"] = scopes
        else:
            body["reason"] = args.reason or ""
        r = await client.post(f"{base}/social/pending/{path}", json=body)
        if r.status_code != 200:
            return await fail(r, "处理待办")
        data = r.json()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            state = (data.get("approval") or {}).get("state", "")
            mark = c("✓", "green") if state == "approved" else c("✗", "yellow")
            print(f"{mark} {target} → {state}")
        return 0

    if sub == "friends":
        r = await client.get(f"{base}/social/relations", params=q({"state": "friend"}))
        return await show(r, "获取好友", render_relations)

    if sub == "relations":
        params = {"state": args.state} if args.state else {}
        return await show(await client.get(f"{base}/social/relations", params=q(params)),
                          "获取关系", render_relations)

    if sub in ("inbox", "outbox", "pending"):
        boxes = ["in", "out"] if sub == "pending" else [sub.replace("box", "")]
        for box in boxes:
            r = await client.get(f"{base}/social/requests", params=q({"box": box}))
            if r.status_code != 200:
                return await fail(r, "获取申请")
            data = r.json()
            items = data.get("requests") or []
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
                continue
            title = "待我处理" if box == "in" else "我发出的在途申请"
            print(c(f"── {title}（{len(items)}）──", "bold"))
            for it in items:
                print_social_relation(it, indent=2)
            if not items:
                print(c("   （空）", "dim"))
        return 0

    if sub == "audit":
        r = await client.get(
            f"{base}/social/audit", params={"peer": args.peer, "limit": args.limit}
        )
        return await show(
            r, "获取审计", lambda d: print_social_audit(d.get("audit") or [])
        )

    if sub == "add":
        if not target:
            print(c('用法：social add <agent> --reason "为什么想加"', "red"), file=sys.stderr)
            return 2
        reason = args.reason
        if not reason:
            print(c('申请必须带 --reason "为什么想加"（防骚扰）', "red"), file=sys.stderr)
            return 2
        body = {"to": target, "message": reason, "scopes": scopes}
        r = await client.post(f"{base}/social/requests", json=body)
        if r.status_code != 200:
            return await fail(r, "发送申请")
        print(c(f"已向 {target} 发出好友申请", "green"))
        if args.json:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print_social_relation(r.json(), indent=2)
        return 0

    if sub == "accept":
        if not target:
            print(c("用法：social accept <peer> [--grant peek,chat]", "red"), file=sys.stderr)
            return 2
        r = await client.post(
            f"{base}/social/requests/accept", json=with_as({"peer": target, "scopes": scopes})
        )
        if r.status_code != 200:
            return await fail(r, "同意申请")
        print(c(f"已与 {target} 成为好友", "green"))
        if args.json:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print_social_relation(r.json(), indent=2)
        return 0

    if sub == "reject":
        if not target:
            print(c("用法：social reject <peer> [--reason ...]", "red"), file=sys.stderr)
            return 2
        body = with_as({"peer": target, "reason": args.reason or ""})
        r = await client.post(f"{base}/social/requests/reject", json=body)
        if r.status_code != 200:
            return await fail(r, "拒绝申请")
        print(c(f"已拒绝 {target} 的申请（理由只记在你自己的审计里）", "dim"))
        return 0

    if sub == "cancel":
        if not target:
            print(c("用法：social cancel <peer>", "red"), file=sys.stderr)
            return 2
        r = await client.post(f"{base}/social/requests/cancel", json={"peer": target})
        if r.status_code != 200:
            return await fail(r, "撤回申请")
        print(c(f"已撤回发给 {target} 的申请", "dim"))
        return 0

    if sub == "grant":
        if not target:
            print(c("用法：social grant <peer> --scopes peek,chat,delegate", "red"), file=sys.stderr)
            return 2
        # `--as` 让 owner 代表自己的 agent 授权：delegate 靠这条才授得出去。
        r = await client.patch(
            f"{base}/social/relations/{target}", json={"scopes": scopes or []}, params=q()
        )
        if r.status_code != 200:
            return await fail(r, "调整权限")
        print(c(f"已更新对 {target} 的权限", "green"))
        if args.json:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print_social_relation(r.json(), indent=2)
        return 0

    if sub == "revoke":
        if not target:
            print(c("用法：social revoke <peer>", "red"), file=sys.stderr)
            return 2
        r = await client.delete(f"{base}/social/relations/{target}", params=q())
        if r.status_code != 200:
            return await fail(r, "解除好友")
        print(c(f"已解除与 {target} 的好友关系（历史记录保留）", "dim"))
        return 0

    if sub == "block":
        if not target:
            print(c("用法：social block <peer> [--undo]", "red"), file=sys.stderr)
            return 2
        url = f"{base}/social/relations/{target}/block"
        r = await (client.delete(url, params=q()) if args.undo else client.post(url, params=q()))
        if r.status_code != 200:
            return await fail(r, "解除拉黑" if args.undo else "拉黑")
        print(c(f"已{'解除对 ' + target + ' 的拉黑' if args.undo else '拉黑 ' + target + '（只有你能解除）'}",
                "dim" if args.undo else "yellow"))
        return 0

    print(c(f"未知子命令 `{sub}`", "red"), file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


def resolve_public_url(
    default_url: str, host: str, port: int, explicit: Optional[str] = None
) -> str:
    """决定写进 Agent Card 的对外地址。

    优先级：``--public-url`` > ``A2A_PUBLIC_URL`` 环境变量 > 按实际监听地址推导。
    不做最后一步推导的话，``serve --port 9000`` 的卡片里会写着默认的 8080，
    对端按卡片地址回连必然失败。
    """
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("A2A_PUBLIC_URL"):
        return default_url.rstrip("/")
    shown = "localhost" if host in ("", "0.0.0.0", "::") else host
    return f"http://{shown}:{port}"


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    s.public_url = resolve_public_url(s.public_url, host, port, args.public_url)

    print(BANNER)
    print(f"  {c('Agent Card', 'cyan')}  {s.public_url.rstrip('/')}/.well-known/agent.json")
    print(f"  {c('JSON-RPC', 'cyan')}    POST {s.public_url.rstrip('/')}/")
    print(f"  {c('控制台', 'cyan')}      {s.public_url.rstrip('/')}/console")
    print(f"  {c('监听', 'cyan')}        http://{host}:{port}")
    print()

    if args.reload:
        uvicorn.run(
            "a2a_hub.server:app",
            host=host,
            port=port,
            reload=True,
            log_level=s.log_level.lower(),
        )
    else:
        from .server import app

        uvicorn.run(app, host=host, port=port, log_level=s.log_level.lower())
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="a2a-hub",
        description="A2A Hub —— 异构 AI Agent 互联互通与多智能体协同网关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    p.add_argument("--version", action="version", version="a2a-hub 0.5.0")

    sub = p.add_subparsers(dest="cmd", required=True)

    # serve
    sp = sub.add_parser("serve", help="启动 HTTP 服务与控制台")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--public-url", dest="public_url", default=None,
                    help="对外可达地址，会写进 Agent Card")
    sp.add_argument("--reload", action="store_true", help="开发模式热重载")

    # agents
    sp = sub.add_parser("agents", help="列出 agent 与健康状态")
    sp.add_argument("--refresh", action="store_true", help="强制重新探测健康状态")
    sp.add_argument("--json", action="store_true", help="输出原始 JSON")

    # health
    sub.add_parser("health", help="探测所有 agent 健康状态")

    # card
    sp = sub.add_parser("card", help="打印 Agent Card")
    sp.add_argument("--agent", default=None, help="指定 agent，缺省为 Hub 自身的卡片")

    # modes
    sub.add_parser("modes", help="列出协同模式")

    # capabilities —— 能力清单（「各 agent 怎么套用」的单一事实来源）
    sp = sub.add_parser("capabilities", help="列出 Hub 的对外能力与三种接法")
    sp.add_argument("--json", action="store_true", help="机器可读输出")

    # attach —— 生成某个 agent 的接入产物
    sp = sub.add_parser("attach", help="生成某个 agent 的接入包（可直接粘贴或写入文件）")
    sp.add_argument("agent", nargs="?", default=None,
                    help="目标 agent id；--transport auto 时用它判断该给哪种通道")
    sp.add_argument("--transport", choices=["auto", "mcp", "cli", "http", "prompt"],
                    default="auto",
                    help="mcp=贴进 MCP host · cli=能跑 shell 的 agent · "
                         "http=云端 agent · prompt=写进系统提示词")
    sp.add_argument("--out", default=None,
                    help="写入文件（用标记块包裹，幂等：重复执行只更新该块，不动其他内容）")
    sp.add_argument("--base-url", dest="base_url", default=None,
                    help="HTTP 方式用的对外地址（默认取 A2A_PUBLIC_URL）")
    sp.add_argument("--as", dest="as_member", default=None,
                    help="身份标识，写进 prompt 片段（如 agent:codex）")
    sp.add_argument("--all", action="store_true",
                    help="给 agents.yaml 里每个 agent 各出一份接入包（按各自的「手」选通道）")
    sp.add_argument("--register", action="store_true",
                    help="MCP 通道：直接把配置合并进 host 的 mcp.json（幂等 + 写前备份）")
    sp.add_argument("--host", default=None,
                    help="MCP host：workbuddy（默认）/ claude / cursor / codex")

    # doctor —— 就绪体检（「拿来就能用」的验收）
    sp = sub.add_parser("doctor", help="体检：能不能调 Hub + 能不能被 Hub 调")
    sp.add_argument("--json", action="store_true", help="机器可读输出")
    sp.add_argument("--no-probe", dest="probe", action="store_false", default=True,
                    help="跳过端到端往返探针（只做静态检查，不启动任何子进程）")
    sp.add_argument("--host", default=None, help="要检查的 MCP host（默认 workbuddy）")

    # ask
    sp = sub.add_parser("ask", help="向 agent 发起一次任务")
    sp.add_argument("prompt", help="任务内容")
    sp.add_argument("--agent", default=None, help="指定 agent，缺省按能力自动路由")
    sp.add_argument("--as", dest="as_member", default=None,
                    help="以哪个成员的身份发起（进程内多成员部署时用；门禁开启后需要 delegate）")

    # collab
    sp = sub.add_parser("collab", help="发起多 agent 协同")
    sp.add_argument("mode", choices=["delegate", "broadcast", "pipeline", "roundtable"])
    sp.add_argument("prompt", help="协同任务")
    sp.add_argument("--agents", default=None, help="指定参与 agent，逗号分隔")
    sp.add_argument("--rounds", type=int, default=None, help="圆桌模式轮数")
    sp.add_argument("--top-k", dest="top_k", type=int, default=None, help="自动选取的 agent 数量")
    sp.add_argument("--synthesizer", default=None, help="结果综合者 agent id")
    sp.add_argument("--reviewer", default=None, help="委派模式下的评审 agent id")
    sp.add_argument("--as", dest="as_member", default=None,
                    help="以哪个成员的身份发起（进程内多成员部署时用；门禁开启后需要 delegate）")
    sp.add_argument("--json", action="store_true", help="机器可读输出")

    # im —— 会话层（像微信一样聊）
    sp = sub.add_parser("im", help="会话层：像微信一样与 agent 聊天与协同")
    sp.add_argument(
        "im_cmd",
        choices=["contacts", "open", "group", "say", "log", "chat"],
        help="contacts=通讯录 · open=打开单聊 · group=建群 · say=发消息 · log=看记录 · chat=单聊一步到位",
    )
    sp.add_argument("agent", nargs="?", default=None, help="open / chat 的目标 agent")
    sp.add_argument("text", nargs="?", default=None, help="消息内容")
    sp.add_argument("--members", default=None, help="群成员，逗号分隔（group 用）")
    sp.add_argument("--title", default=None, help="群名称")
    sp.add_argument("-c", "--conversation", dest="conversation", default=None,
                    help="会话 id（say / log 用）")
    sp.add_argument("--wait", action="store_true", help="发完等所有回复再退出")
    sp.add_argument("--refresh", action="store_true", help="先刷新 agent 健康状态")
    sp.add_argument("--as", dest="as_member", default=None,
                    help="以某个成员身份查看（owner 看自己 agent 的视角，走 --url 时生效）")
    sp.add_argument("--timeout", type=float, default=120.0, help="等待回复的超时秒数")
    sp.add_argument("--limit", type=int, default=200, help="打印的消息条数上限")

    # social —— 社交层（加好友才能交流）
    sp = sub.add_parser("social", help="社交层：成员、好友关系与交流权限")
    sp.add_argument(
        "social_cmd",
        choices=[
            "me", "find", "profile", "discover", "add", "inbox", "outbox", "pending",
            "accept", "reject", "cancel", "friends", "relations", "grant", "revoke",
            "block", "audit", "introduce", "intro", "introductions",
            "need", "approvals", "approve", "deny",
            "init",
        ],
        help="me=我的名片 · find=搜人 · profile=看某人的名片（只有共同好友数） · "
             "discover=按匹配度发现值得认识的人 · add=发申请 · inbox/outbox=收发件箱 · "
             "accept/reject/cancel=处理申请 · friends/relations=好友与关系 · "
             "grant=改权限 · revoke=删好友 · block=拉黑 · audit=审计链 · "
             "introduce=引荐 · intro=别人引荐给我的 · "
             "need=报告能力缺口（自主交友） · approvals/approve/deny=自主待办 · "
             "init=一键启用（生成 config/members.yaml，幂等不覆盖）",
    )
    sp.add_argument("target", nargs="?", default=None,
                    help="对方成员 id（add/accept/reject/cancel/grant/revoke/block/profile/"
                         "introduce 用），或 find/discover 的搜索关键词，或 approve/deny 的待办 id")
    sp.add_argument("--reason", "--message", dest="reason", default=None,
                    help="申请理由（add 必填，防骚扰；reject/deny 时可写明原因）")
    sp.add_argument("--scopes", "--grant", dest="scopes", default=None,
                    help="权限列表，逗号分隔（如 peek,chat,delegate）")
    sp.add_argument("--state", default=None,
                    help="relations 过滤：none/pending/friend/rejected/blocked")
    sp.add_argument("--peer", default=None, help="audit 只看与某人的往来")
    sp.add_argument("--need", default=None,
                    help="discover 的能力缺口（如「OCR 表格提取」），决定技能互补分")
    sp.add_argument("--to", dest="to", default=None, help="introduce：引荐给谁")
    sp.add_argument("--note", default=None, help="introduce：推荐语（会出现在对方看到的引荐里）")
    sp.add_argument("--as", dest="as_member", default=None,
                    help="以某个成员身份操作（owner 代表自己的 agent 时用）")
    sp.add_argument("--undo", action="store_true", help="block 时表示解除拉黑")
    sp.add_argument("--limit", type=int, default=50, help="条数上限（find/discover/audit 用）")
    sp.add_argument("--json", action="store_true", help="输出原始 JSON")

    for sub_p in (sub.choices["agents"], sub.choices["health"], sub.choices["card"],
                  sub.choices["modes"], sub.choices["ask"], sub.choices["collab"],
                  sub.choices["im"], sub.choices["social"]):
        sub_p.add_argument("--url", default=None, help="远端 Hub 地址（不给则进程内执行）")
        sub_p.add_argument("--token", default=None, help="Bearer Token")
        # 只有 agents / social 子命令声明了 --json，其余仅设置默认值避免 args.json 缺失
        sub_p.set_defaults(json=False)

    return p


# --------------------------------------------------------------------------- #
# 能力清单与接入包（纯本地）
# --------------------------------------------------------------------------- #

#: 各生态默认套用哪种通道。判断依据是「这个 agent 有什么手」：
#: 支持 MCP 的走 MCP（一次配置长期有效），云端 agent 只能发 HTTP，
#: 其余（脚本型 / 本地模型）给命令行。
_AUTO_TRANSPORT = {
    "claude_code": "mcp",
    "codex": "mcp",
    "workbuddy": "mcp",
    "coze": "http",
    "openai_compat": "http",
    # 别人的 A2A 服务：它本身就是个 HTTP 端点
    "remote_a2a": "http",
    # 回显 agent 没有「手」，但它常被当自检样本跑 shell，给 cli 最合适
    "echo": "cli",
}
_DEFAULT_TRANSPORT = "cli"


def _cmd_capabilities(args: argparse.Namespace) -> int:
    """``capabilities`` —— 打印 Hub 的对外能力与三种接法。"""
    from .capabilities import manifest, render_table

    if getattr(args, "json", False):
        print(
            json.dumps(
                manifest(base_url=getattr(args, "base_url", "") or ""),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print("A2A Hub 能力清单 —— 同一份清单，按 agent 的「手」选一种接法：")
    print(render_table())
    print()
    print("接入某个 agent：python run.py attach <agent-id> [--transport mcp|cli|http|prompt]")
    return 0


def _guess_transport(agent_id: Optional[str]) -> str:
    """按 ``agents.yaml`` 里声明的 type 猜该给哪种通道。

    猜不到就退回 ``cli``——**能跑命令的 agent 最多**，这个默认最不容易给错。
    """
    if not agent_id:
        return _DEFAULT_TRANSPORT
    try:
        from .config import HubConfig

        cfg = HubConfig.load(get_settings().agents_path())
    except Exception:  # noqa: BLE001 - 只是猜个默认值，读不到就算了
        return _DEFAULT_TRANSPORT
    for a in cfg.agents:
        if a.id == agent_id:
            return _AUTO_TRANSPORT.get(a.type, _DEFAULT_TRANSPORT)
    return _DEFAULT_TRANSPORT


def _write_marked_block(path: str, text: str) -> None:
    """把产物写进文件的标记块里。

    **幂等**：重复执行只替换自己那一块，不动用户原有的内容——
    往 ``CLAUDE.md`` / ``AGENTS.md`` 这种「用户自己的文件」里写东西，
    必须能反复执行而不堆叠。
    """
    from pathlib import Path

    from .capabilities import MARK_BEGIN, MARK_END

    block = f"{MARK_BEGIN}\n{text}\n{MARK_END}"
    p = Path(path)
    if p.exists():
        old = p.read_text(encoding="utf-8")
        if MARK_BEGIN in old and MARK_END in old:
            s = old.index(MARK_BEGIN)
            e = old.index(MARK_END) + len(MARK_END)
            new = old[:s] + block + old[e:]
        else:
            new = old.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    p.write_text(new, encoding="utf-8")


def _agent_ids() -> list[str]:
    """列出 ``agents.yaml`` 里声明的 agent id（读不到就返回空）。"""
    try:
        from .config import HubConfig

        cfg = HubConfig.load(get_settings().agents_path())
    except Exception:  # noqa: BLE001 - 只是列个名单，读不到就算了
        return []
    return [a.id for a in cfg.agents]


def _register_mcp(args: argparse.Namespace, transport: str) -> int:
    """把 MCP 配置真正写进 host 的配置文件（幂等 + 写前备份）。

    这一步是「说明」与「装好」的分界线：attach 的其余形态都只是给人看的文本，
    只有它会让机器状态发生变化。
    """
    from . import attach_kit

    if transport != "mcp":
        print(c(f"--register 只对 MCP 通道有意义（当前推断为 {transport}）；"
                f"确实要登记 MCP 就显式加 --transport mcp", "yellow"),
              file=sys.stderr)
        return 2

    host_key = args.host or attach_kit.DEFAULT_HOST
    res = attach_kit.register(host_key)
    if not res.get("ok"):
        print(c(res.get("error", "登记失败"), "red"), file=sys.stderr)
        if res.get("path"):
            print(c(f"  文件：{res['path']}", "dim"), file=sys.stderr)
        return 1

    if res["changed"]:
        print(c(f"{'已创建' if res['created'] else '已更新'} {res['path']}", "green"))
        if res["backup"]:
            print(c(f"  原文件已备份 → {res['backup']}", "dim"))
    else:
        print(c(f"{res['path']} 里已是当前配置，无需改动。", "green"))
    if res.get("note"):
        print(c(f"  {res['note']}", "dim"))
    print(c("  验证：python run.py doctor", "dim"))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``doctor`` —— 体检「能不能调 Hub」+「能不能被 Hub 调」。"""
    from . import attach_kit, doctor

    report = asyncio.run(doctor.run(
        get_settings(),
        probe=args.probe,
        host=args.host or attach_kit.DEFAULT_HOST,
    ))
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(doctor.render(report))
    return 0 if report.ok else 1


def _cmd_attach(args: argparse.Namespace) -> int:
    """``attach`` —— 生成某个 agent 的接入包（可直接粘贴 / 写入 / 登记）。"""
    from .capabilities import render_attach

    base = args.base_url or get_settings().public_url

    # --all：一次拿到全套——每个 agent 按自己的「手」选通道
    if args.all:
        ids = _agent_ids()
        if not ids:
            print(c("读不到 agents.yaml，无法 --all", "red"), file=sys.stderr)
            return 1
        for i, aid in enumerate(ids):
            if i:
                print("\n" + "═" * 64 + "\n")
            tr = _guess_transport(aid)
            print(c(f"【{aid}】按 type 推断通道：{tr}", "magenta"))
            print(render_attach(tr, identity=f"agent:{aid}", base_url=base))
        return 0

    transport = args.transport
    if transport == "auto":
        transport = _guess_transport(args.agent)
    identity = args.as_member or (f"agent:{args.agent}" if args.agent else "")
    text = render_attach(transport, identity=identity, base_url=base)

    # --register：把「说明」变成「装好」
    if args.register:
        return _register_mcp(args, transport)

    if args.out:
        try:
            _write_marked_block(args.out, text)
        except OSError as exc:
            print(c(f"写入 {args.out} 失败：{exc}", "red"), file=sys.stderr)
            return 1
        print(c(f"已写入 {args.out}", "green"))
        head = f"  通道：{transport}"
        if args.agent:
            head += f" · 目标：{args.agent}"
        print(head)
        print("  重复执行只更新标记块，不会覆盖文件里的其他内容。")
        return 0

    print(text)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "serve":
        return _serve(args)

    # 能力清单与接入包是**纯本地**的：清单就在本仓库里，不需要连 Hub，
    # 所以不走 async runner（也就不受 --url / --token 影响）。
    if args.cmd == "capabilities":
        return _cmd_capabilities(args)
    if args.cmd == "attach":
        return _cmd_attach(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)

    runner = _remote_main if getattr(args, "url", None) else _inproc_main
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
