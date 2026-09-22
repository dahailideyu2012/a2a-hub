#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A2A Hub —— MCP stdio 服务器（零第三方依赖）

把 A2A Hub 的能力暴露成 MCP 工具，让本机 WorkBuddy 可以直接：

    a2a_agents       Hub 上有哪些 agent、各自会什么、现在能不能用
    a2a_route        这段任务该派给谁（只打分，不执行）
    a2a_delegate     派活给某个 agent / 自动路由，等结果回来
    a2a_collab       多 agent 协同（pipeline / parallel / debate / router）
    a2a_task         查任务状态、取回结果
    a2a_social       社交只读：我是谁 / 好友 / 发现 / 申请箱 / 待办
    a2a_social_act   社交写操作：申请 / 同意 / 拒绝 / 授权 / 拉黑（需 confirm）
    a2a_social_init  一键启用社交网络：生成 members.yaml 并热启用（幂等，不覆盖）

设计取舍
--------
**走进程内的 `Hub`，而不是打 HTTP。** 这样不要求 Hub 先 `serve`、不用配 token，
而且社交门禁、简报注入、事件总线全是同一套实例——不会出现「MCP 看到的状态」
和「Hub 真实状态」不一致。代价是 MCP server 必须由装了项目依赖的解释器拉起。

**写操作要 confirm。** 改关系 / 授执行权这类动作，缺 `confirm=true` 一律拒绝，
先把「将要做什么」回给调用方，等人在会话里明确同意后再执行。

stdout 只允许出现 JSON-RPC，日志一律走 stderr。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Optional

# MCP 客户端会以任意 cwd 拉起进程 —— 必须在 import a2a_hub 之前修好 sys.path
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED = {"2024-11-05", "2025-03-26", "2025-06-18"}
TERMINAL_STATES = {"completed", "failed", "canceled"}

SERVER_INFO = {"name": "a2a-hub", "version": "0.5.0"}

_hub: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #


def _log(msg: str) -> None:
    sys.stderr.write(f"[a2a-hub] {msg}\n")
    sys.stderr.flush()


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _run(coro: Any) -> Any:
    """在**同一个** event loop 上跑协程。

    Hub 内部的事件总线、SSE 资源都绑在 loop 上，每次新建 loop 会让
    异步资源换主人，行为变得不可预测。
    """
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)


def _get_hub() -> Any:
    global _hub
    if _hub is None:
        from a2a_hub.server import hub  # 模块级已装配好的 Hub 实例

        _hub = hub
        _log("Hub 已装载")
    return _hub


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _fmt(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _artifact_text(task: dict[str, Any]) -> str:
    """从 task 里把 agent 真正产出的文本拼出来。

    artifacts 是「同名追加」语义（见项目约定 6），所以要遍历所有 part。
    """
    parts: list[str] = []
    for art in task.get("artifacts") or []:
        for p in art.get("parts") or []:
            if p.get("kind") == "text" or "text" in p:
                parts.append(p.get("text", ""))
    return "\n".join(parts).strip()


async def _dispatch(method: str, params: dict[str, Any], agent: Optional[str] = None) -> Any:
    hub = _get_hub()
    req = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params}
    return await hub.dispatcher.handle(req, agent, "user")


def _unwrap(resp: Any) -> dict[str, Any]:
    """把 JSON-RPC 响应拆成 result；出错时抛出带错误码的可读异常。"""
    if not isinstance(resp, dict):
        raise RuntimeError(f"非预期响应：{resp!r}")
    if "error" in resp:
        e = resp["error"]
        hint = (e.get("data") or {}).get("hint")
        msg = f"[{e.get('code')}] {e.get('message')}"
        if hint:
            msg += f"\n提示：{hint}"
        raise RuntimeError(msg)
    return resp.get("result") or {}


def tool_agents(args: dict[str, Any]) -> dict[str, Any]:
    resp = _run(_dispatch("agents/list", {}))
    try:
        result = _unwrap(resp)
    except RuntimeError as e:
        return _err(str(e))
    items = result.get("agents") or result.get("items") or result
    if not isinstance(items, list):
        return _ok(_fmt(result))

    lines = []
    for a in items:
        name = a.get("id") or a.get("name")
        state = a.get("state") or ("可用" if a.get("available", True) else "不可用")
        skills = ", ".join(
            (s.get("name") or s.get("id") or "") for s in (a.get("skills") or [])
        )
        auto = "" if a.get("autoRoute", True) else " [需点名]"
        lines.append(f"- {name}  ({state}){auto}")
        if skills:
            lines.append(f"    能力：{skills}")
    return _ok("\n".join(lines) or "（Hub 上没有 agent）")


def tool_route(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _err("缺少 query")
    top_k = int(args.get("top_k") or 5)
    hub = _get_hub()
    ranked = hub.registry.rank(query, top_k=top_k, only_enabled=True)
    if not ranked:
        return _ok("没有 agent 能接这个任务（可能都被禁用或 auto_route=false）")
    lines = [f"任务：{query}", ""]
    for i, (rec, score) in enumerate(ranked, 1):
        mark = "首选" if i == 1 else "次选"
        lines.append(f"{mark}  {rec.id:<12} {score:6.1f}   {rec.spec.name or ''}")
    return _ok("\n".join(lines))


def tool_delegate(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("缺少 text")
    agent = args.get("agent")
    timeout = float(args.get("timeout") or 180)

    if not agent:
        ranked = _get_hub().registry.rank(text, top_k=1, only_enabled=True)
        if not ranked:
            return _err("没有可用 agent 能接这个任务")
        agent = ranked[0][0].id

    params = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}}
    try:
        resp = _run(_dispatch("message/send", params, agent))
        result = _unwrap(resp)
    except RuntimeError as e:
        return _err(f"派发失败：{e}")

    task = result.get("task") or result
    task_id = task.get("id")

    # 多数适配器是同步跑完的；没跑完就轮询，别让调用方自己去查。
    deadline = time.time() + timeout
    while task.get("status", {}).get("state") not in TERMINAL_STATES and time.time() < deadline:
        time.sleep(1.0)
        try:
            got = _unwrap(_run(_dispatch("tasks/get", {"id": task_id})))
            task = got.get("task") or got
        except RuntimeError as e:
            return _err(f"轮询任务失败：{e}")

    state = task.get("status", {}).get("state", "unknown")
    body = _artifact_text(task)
    head = f"agent={agent}  task={task_id}  state={state}"
    if state == "failed":
        return _err(f"{head}\n{fmt_status_msg(task)}")
    if not body:
        body = fmt_status_msg(task)
    return _ok(f"{head}\n\n{body}")


def fmt_status_msg(task: dict[str, Any]) -> str:
    st = task.get("status") or {}
    msg = st.get("message")
    if isinstance(msg, dict):
        parts = msg.get("parts") or []
        return "\n".join(p.get("text", "") for p in parts).strip() or str(msg)
    return str(msg) if msg else "（无文本输出）"


def tool_collab(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("缺少 text")
    params: dict[str, Any] = {"text": text}
    if args.get("mode"):
        params["mode"] = args["mode"]
    if args.get("agents"):
        params["agents"] = args["agents"]
    try:
        result = _unwrap(_run(_dispatch("collab/run", params)))
    except RuntimeError as e:
        return _err(f"协同启动失败：{e}")
    return _ok(_fmt(result))


def tool_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _err("缺少 task_id")
    try:
        result = _unwrap(_run(_dispatch("tasks/get", {"id": task_id})))
    except RuntimeError as e:
        return _err(str(e))
    task = result.get("task") or result
    state = task.get("status", {}).get("state", "unknown")
    return _ok(f"state={state}\n\n{_artifact_text(task) or fmt_status_msg(task)}")


READ_ACTIONS = {
    "me": ("social/me", {}),
    "members": ("social/members", {}),
    "discover": ("social/discover", {}),
    "introductions": ("social/introductions", {}),
    "relations": ("social/relations", {}),
    "requests": ("social/requests", {}),
    "pending": ("social/pending", {}),
}

WRITE_ACTIONS = {
    "request": "social/request",
    "accept": "social/accept",
    "reject": "social/reject",
    "grant": "social/grant",
    "revoke": "social/revoke",
    "block": "social/block",
    "need": "social/need",
    "introduce": "social/introduce",
    "approve": "social/approve",
    "deny": "social/deny",
}


def _autoininit_on() -> bool:
    """社交工具被调用而门禁还没开时，是否自动一键启用（默认开）。

    关掉它就退回「必须先手工准备 members.yaml」的老行为：
    ``A2A_SOCIAL_AUTOINIT=false``。
    """
    return os.getenv("A2A_SOCIAL_AUTOINIT", "true").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _ensure_social() -> tuple[bool, str]:
    """保证社交层可用，返回 ``(可用?, 说明)``。

    未启用时会**就地热启用**（生成 members.yaml + 重载同一个图对象），
    所以调用方不用重启 Hub，也不用手工复制配置文件。
    """
    try:
        hub = _get_hub()
    except Exception as e:  # noqa: BLE001 - Hub 起不来要变成可读错误，不是崩溃
        return False, f"Hub 装载失败：{type(e).__name__}: {e}"
    g = getattr(hub, "social_graph", None)
    if g is not None and g.enabled:
        return True, ""
    if not _autoininit_on():
        return False, (
            "社交层未启用（缺 config/members.yaml），且 A2A_SOCIAL_AUTOINIT=false。"
            "执行 `python run.py social init` 或调用 a2a_social_init 后即可使用，无需重启。"
        )
    try:
        state = hub.ensure_social(auto_init=True)
    except Exception as e:  # noqa: BLE001
        return False, f"一键启用社交层失败：{type(e).__name__}: {e}"
    if not state.get("enabled"):
        return False, state.get("reason") or "一键启用社交层失败"
    return True, state.get("reason") or "社交层已启用"


def tool_social_init(args: dict[str, Any]) -> dict[str, Any]:
    """一键启用社交层：生成 ``members.yaml`` 并**热启用**，不用重启。

    幂等：文件已存在就原样返回，绝不覆盖用户手改过的配置。
    """
    try:
        hub = _get_hub()
    except Exception as e:  # noqa: BLE001
        return _err(f"Hub 装载失败：{type(e).__name__}: {e}")
    try:
        state = hub.ensure_social(auto_init=True)
    except Exception as e:  # noqa: BLE001
        return _err(f"一键启用失败：{type(e).__name__}: {e}")
    if not state.get("enabled"):
        return _err(state.get("reason") or "一键启用失败")
    members = state.get("members") or []
    text = (
        f"社交层已启用（未重启，就地热启用）。\n"
        f"成员表：{state.get('path')}\n"
        f"说明：{state.get('reason')}\n"
        f"成员数：{len(members)}\n"
        + ("成员：" + ", ".join(str(m) for m in members[:20]) if members else "")
    )
    return _ok(text)


def tool_social(args: dict[str, Any]) -> dict[str, Any]:
    action = (args.get("action") or "me").strip()
    if action not in READ_ACTIONS:
        return _err(f"未知只读动作 {action!r}，可选：{', '.join(READ_ACTIONS)}")
    ok, note = _ensure_social()
    if not ok:
        return _err(note)
    method, base = READ_ACTIONS[action]
    params = dict(base)
    if args.get("member"):
        params["member"] = args["member"]
        params["memberId"] = args["member"]  # 兼容两种命名
    if args.get("query"):
        params["query"] = args["query"]
    try:
        result = _unwrap(_run(_dispatch(method, params)))
    except RuntimeError as e:
        return _err(str(e))
    body = _fmt(result) if result else f"（{action} 暂无数据）"
    return _ok(f"{note}\n\n{body}" if note else body)


def tool_social_act(args: dict[str, Any]) -> dict[str, Any]:
    action = (args.get("action") or "").strip()
    if action not in WRITE_ACTIONS:
        return _err(f"未知写动作 {action!r}，可选：{', '.join(WRITE_ACTIONS)}")
    ok, note = _ensure_social()
    if not ok:
        return _err(note)
    member = args.get("member")
    params: dict[str, Any] = {}
    if member:
        params["member"] = member
        params["memberId"] = member
    if args.get("scopes"):
        params["scopes"] = args["scopes"]
    if args.get("reason"):
        params["reason"] = args["reason"]
    if args.get("need"):
        params["need"] = args["need"]
    if args.get("id"):
        params["id"] = args["id"]

    # 写闸门：不 confirm 就只回「将要做什么」，不落任何变更
    prefix = f"{note}\n\n" if note else ""
    if not args.get("confirm"):
        return _ok(
            prefix
            + "未执行（需要 confirm=true 才会真正改动）。\n"
            f"将要执行：{action} {member or ''} {json.dumps(params, ensure_ascii=False)}\n"
            "确认无误后，用同样参数并带上 confirm=true 再调用一次。"
        )
    try:
        result = _unwrap(_run(_dispatch(WRITE_ACTIONS[action], params)))
    except RuntimeError as e:
        return _err(str(e))
    return _ok(prefix + (_fmt(result) if result else f"{action} 已执行"))


TOOL_FUNCS = {
    "a2a_agents": tool_agents,
    "a2a_route": tool_route,
    "a2a_delegate": tool_delegate,
    "a2a_collab": tool_collab,
    "a2a_task": tool_task,
    "a2a_social": tool_social,
    "a2a_social_act": tool_social_act,
    "a2a_social_init": tool_social_init,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "a2a_agents",
        "description": (
            "列出 A2A Hub 上注册的所有 agent：名字、能力与当前是否可用。"
            "用它在派活前确认「谁能干活」。只读、秒回。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "a2a_route",
        "description": (
            "给一段任务描述，返回 Hub 认为该派给谁（按能力打分排序）。"
            "只做判断、不执行任何任务，适合在派活前先确认人选。只读、秒回。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "任务描述文本"},
                "top_k": {"type": "integer", "description": "返回几个候选，默认 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "a2a_delegate",
        "description": (
            "把任务派给 Hub 上的某个 agent 并等待它跑完，返回它产出的文本。"
            "不指定 agent 时由 Hub 按能力自动挑人。"
            "注意：这会真实唤起对应 agent（可能调用付费 API 或执行本地命令）、"
            "耗时可达数分钟，且会消耗对额度；调用前应先向用户说明要派给谁。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "交给 agent 的任务内容"},
                "agent": {
                    "type": "string",
                    "description": "目标 agent id（如 codex、claude-code、qwen-office）；留空则自动路由",
                },
                "timeout": {"type": "number", "description": "最长等待秒数，默认 180"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "a2a_collab",
        "description": (
            "发起一次多 agent 协同：pipeline（串行接力）/ parallel（并行汇总）/"
            " debate（辩论）/ router（路由分发）。适合单干不好的复杂任务。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "总任务描述"},
                "mode": {"type": "string", "description": "pipeline | parallel | debate | router"},
                "agents": {"type": "array", "items": {"type": "string"}, "description": "限定参与的 agent id"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "a2a_task",
        "description": "按 task_id 查询任务状态，并取回该 agent 已产出的文本结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务 id"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "a2a_social_init",
        "description": (
            "一键启用社交网络：自动生成 config/members.yaml（默认人类 + 所有本地 agent，"
            "均归到你名下）并立即热启用，**不需要手工复制配置文件，也不需要重启服务**。"
            "幂等——文件已存在时原样返回，绝不覆盖你手改过的配置。"
            "当 a2a_social / a2a_social_act 报告「社交层未启用」时先调它。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "a2a_social",
        "description": (
            "只读地查看 Hub 社交网络：me（我的名片与关系概览）/ members（搜索成员）/ "
            "discover（按匹配度推荐新朋友）/ introductions（别人引荐给我的）/ "
            "relations（我的好友）/ requests（好友申请箱）/ pending（待我拍板的自主交友待办）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "me | members | discover | introductions | relations | requests | pending",
                },
                "member": {"type": "string", "description": "目标成员 id（members/discover 等可用）"},
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "a2a_social_act",
        "description": (
            "社交写操作：request（发好友申请）/ accept / reject / grant（调整好友权限，"
            "授予 delegate 才允许派活）/ revoke / block / need（报告能力缺口）/ "
            "introduce / approve / deny。"
            "安全闸门：不带 confirm=true 时不会做任何改动，只回显「将要做什么」；"
            "必须先把这个计划展示给用户、获得明确同意后，再带 confirm=true 调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "request | accept | reject | grant | revoke | block | need | introduce | approve | deny"},
                "member": {"type": "string", "description": "目标成员 id"},
                "scopes": {"type": "array", "items": {"type": "string"}, "description": "grant 时授予的权限档位"},
                "reason": {"type": "string", "description": "申请/引荐理由"},
                "need": {"type": "string", "description": "need 动作的能力缺口描述"},
                "id": {"type": "string", "description": "approve/deny 时的待办 id"},
                "confirm": {"type": "boolean", "description": "必须为 true 才会真正执行"},
            },
            "required": ["action"],
        },
    },
]


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return _err(f"未知工具：{name}。可用：{', '.join(TOOL_FUNCS)}")
    try:
        return fn(args or {})
    except Exception as e:  # noqa: BLE001 - 工具异常必须转成可读结果，不能炸连接
        _log(f"tool {name} error: {e!r}")
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 协议循环
# --------------------------------------------------------------------------- #


def _handle(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:  # 通知，绝不回包
        return
    params = msg.get("params") or {}

    if method == "initialize":
        v = params.get("protocolVersion")
        _write(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": v if v in SUPPORTED else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    # 给 host / 模型的「这是什么、怎么用」——接入方不必先读文档。
                    # 保持简短：这段会进模型上下文。
                    "instructions": (
                        "A2A Hub —— 本机多智能体协作网关。"
                        "先 a2a_agents 看谁在线、会什么（别凭印象点人）；"
                        "a2a_delegate 派活（不指定 agent 会按能力自动路由）；"
                        "a2a_collab 做多 agent 协同；a2a_task 查长任务进度；"
                        "a2a_social / a2a_social_act 管好友与权限（写操作需 confirm）；"
                        "a2a_social_init 一键启用社交层。"
                        "注意：派活会真实占用该 agent 的时间与额度，"
                        "且对方可能需要先授予 delegate 权限。"
                    ),
                },
            }
        )
    elif method == "tools/list":
        _write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        _write(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": call_tool(params.get("name", ""), params.get("arguments") or {}),
            }
        )
    elif method == "ping":
        _write({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        _write(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )


def main() -> None:
    for s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    _log("MCP server 启动")

    for raw in sys.stdin:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            _log(f"bad json: {e}")
            continue  # 单条脏数据不能让服务退出
        if not isinstance(msg, dict):
            # 合法 JSON 但不是对象（裸字符串/数字/数组）。后面所有 msg.get(...)
            # 都会 AttributeError —— 包括异常分支里的那次，等于把进程崩掉。
            _log(f"skip non-object message: {type(msg).__name__}")
            continue
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001
            _log(f"handler error: {e!r}")
            if msg.get("id") is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32603, "message": str(e)},
                    }
                )


if __name__ == "__main__":
    main()
