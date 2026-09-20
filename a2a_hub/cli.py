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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
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
    orch = Orchestrator(reg, reg.bus)

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
    run = orch.create_run(args.mode, args.prompt, agent_ids, options)
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
    await orch.run_sync(run)
    await asyncio.wait_for(watcher, timeout=5)

    print()
    print(c("══ 协同结果 ══", "bold"))
    print(run.result or c("(无产出)", "dim"))
    print()
    print(c(f"状态: {run.status} · 步骤: {len(run.steps)}", "dim"))
    return 0 if run.status == "completed" else 1


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

    return 0


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
    p.add_argument("--version", action="version", version="a2a-hub 0.2.0")

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

    # ask
    sp = sub.add_parser("ask", help="向 agent 发起一次任务")
    sp.add_argument("prompt", help="任务内容")
    sp.add_argument("--agent", default=None, help="指定 agent，缺省按能力自动路由")

    # collab
    sp = sub.add_parser("collab", help="发起多 agent 协同")
    sp.add_argument("mode", choices=["delegate", "broadcast", "pipeline", "roundtable"])
    sp.add_argument("prompt", help="协同任务")
    sp.add_argument("--agents", default=None, help="指定参与 agent，逗号分隔")
    sp.add_argument("--rounds", type=int, default=None, help="圆桌模式轮数")
    sp.add_argument("--top-k", dest="top_k", type=int, default=None, help="自动选取的 agent 数量")
    sp.add_argument("--synthesizer", default=None, help="结果综合者 agent id")
    sp.add_argument("--reviewer", default=None, help="委派模式下的评审 agent id")

    for sub_p in (sub.choices["agents"], sub.choices["health"], sub.choices["card"],
                  sub.choices["modes"], sub.choices["ask"], sub.choices["collab"]):
        sub_p.add_argument("--url", default=None, help="远端 Hub 地址（不给则进程内执行）")
        sub_p.add_argument("--token", default=None, help="Bearer Token")
        # 只有 agents 子命令声明了 --json，其余仅设置默认值避免 args.json 缺失
        sub_p.set_defaults(json=False)

    return p


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

    runner = _remote_main if getattr(args, "url", None) else _inproc_main
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
