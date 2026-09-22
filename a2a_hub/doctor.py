"""就绪体检 —— 「拿来就能用」的**验收工具**。

## 它解决什么

接一个新 agent 会踩两类完全不同的坑，而过去没有任何一处把它们并排检查：

1. **能不能调 Hub** —— 这是 `capabilities.py` / `attach` 管的（说明书 + 接入包）。
2. **能不能被 Hub 调** —— 这是 `agents.yaml` 里那张适配器表管的。

第 2 类最阴：配置写好了、命令也贴了，一跑 `run.py agents` 才发现一排
`unavailable · 缺少 api_key`。**接得进来 ≠ 装得好。**

`doctor` 把两侧合成一份体检报告，并且**跑一次真实往返**作为终检——
不是「配置文件里有这一行」，而是「事件流真的从 adapter 回来了」。

## 为什么要真探针

自检最容易自欺：检查「配置项存在」得到绿灯，实际链路早断了
（解释器不对、依赖缺失、子进程命令写错）。所以终检对 `echo` 发一个带
**随机 token** 的任务，只有回显里出现同一个 token 才算过——
这是**可机器验证的**证据，人也伪造不了。

## 探针为什么不走社交门禁

门禁本身是单独一项检查。探针要回答的是「适配器链路通不通」，
如果混进门禁，一个健康的部署会因为「还没加好友」被误报成红的。
所以探针直接走 `registry.execute`，门禁状态另算。
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 探针优先选的 agent（零依赖、永远健康），没有就退到第一个 healthy 的
_PROBE_PREFERRED = ("echo",)

#: 人类可读的下一步模板（按检查项 key 给修复建议）
_FIXES = {
    "env.python": "升级到 Python 3.10+（项目要求），再用该解释器创建 venv 装依赖。",
    "env.agents": "确认 config/agents.yaml 存在且是合法 YAML。",
    "agents.ready": "按上面提示补 .env 里的 key（如 DASHSCOPE_API_KEY），"
                    "或把暂时不用的 agent 设 `enabled: false`。",
    "social": "跑 `python run.py social init` 一键启用社交层（幂等、不覆盖）。",
    "mcp": "跑 `python run.py attach workbuddy --register` 自动登记，"
           "然后到连接器管理页点一次「信任」。",
    "probe": "探针失败通常是适配器或依赖问题：先单独跑 "
             "`python run.py ask echo ping` 看完整报错。",
}


@dataclass
class Check:
    """一条检查项。"""

    key: str
    title: str
    ok: bool
    detail: str = ""
    #: 不通过也不影响「能用」的项（例如社交层未启用）
    optional: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    """体检报告。``ok`` 只看**必需项**，可选项目单独呈现。"""

    checks: list[Check] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    probe: dict[str, Any] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """必需项全过才算过。"""
        return all(c.ok for c in self.checks if not c.optional)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [asdict(c) for c in self.checks],
            "agents": self.agents,
            "probe": self.probe,
            "next_steps": self.next_steps,
        }


def _add(report: Report, check: Check) -> None:
    report.checks.append(check)
    if not check.ok:
        fix = _FIXES.get(check.key)
        if fix and (not check.optional or check.key in ("social", "mcp")):
            report.next_steps.append(f"{check.title}：{fix}")


# --------------------------------------------------------------------------- #
# 各段检查
# --------------------------------------------------------------------------- #


def _check_env(report: Report, settings: Any) -> None:
    v = sys.version_info
    _add(report, Check(
        key="env.python",
        title="运行环境",
        ok=v >= (3, 10),
        detail=f"Python {v.major}.{v.minor}.{v.micro}"
               + ("" if v >= (3, 10) else "（项目要求 3.10+）"),
    ))
    agents_path = Path(settings.agents_path())
    ok = agents_path.exists()
    _add(report, Check(
        key="env.agents",
        title="agent 注册表",
        ok=ok,
        detail=str(agents_path) if ok else f"找不到 {agents_path}",
    ))


def _check_agents(report: Report, records: list[Any]) -> None:
    """把「能不能被 Hub 调」摊开：谁就绪、谁缺什么。

    健康信息在 ``AgentRecord.health`` 字典里（不是 ``rec.status``）——
    它由 ``check_health()`` 用**强制**模式填，否则会命中 60 秒 TTL 缓存、
    全员显示 `尚未探测`。
    """
    ready, blocked = [], []
    for rec in records:
        health = getattr(rec, "health", None) or {}
        st = health.get("status", "unknown")
        detail = health.get("detail", "")
        item = {
            "id": rec.id,
            "name": getattr(rec, "name", rec.id),
            "type": getattr(rec, "type", ""),
            "status": st,
            "detail": detail,
            "ready": st == "healthy",
        }
        report.agents.append(item)
        (ready if item["ready"] else blocked).append(item)

    names = ", ".join(a["id"] for a in blocked) or "无"
    _add(report, Check(
        key="agents.ready",
        title="可被调用的 agent",
        ok=bool(ready),
        detail=f"{len(ready)} 个就绪 / {len(blocked)} 个不可用"
               + (f"（{names}）" if blocked else ""),
        data={"ready": [a["id"] for a in ready],
              "blocked": [a["id"] for a in blocked]},
    ))


def _check_social(report: Report, settings: Any) -> None:
    path = Path(settings.members_path())
    enabled = path.exists()
    mode = getattr(settings, "social_mode", "")
    # 社交层是**可选增强**：没开不影响 Hub 干活
    _add(report, Check(
        key="social",
        title="社交门禁",
        ok=enabled,
        optional=True,
        detail=(f"已启用（{path.name}，mode={mode}）" if enabled
                else f"未启用（没有 {path}）——好友制与权限分档都不生效"),
    ))


def _check_mcp(report: Report, host: str, project_root: Optional[Path] = None) -> None:
    from . import attach_kit

    info = attach_kit.inspect(host, project_root=project_root)
    if not info.get("known"):
        _add(report, Check(
            key="mcp", title="MCP 登记", ok=False, optional=True,
            detail=f"未知 host：{host}",
        ))
        return
    if not info["registered"]:
        _add(report, Check(
            key="mcp",
            title="MCP 登记",
            ok=False,
            optional=True,
            detail=f"{info['label']} 未登记（{info['path']}）",
            data=info,
        ))
        return
    matches = info["command_matches"]
    _add(report, Check(
        key="mcp",
        title="MCP 登记",
        ok=matches,
        optional=True,
        detail=(
            f"{info['label']} 已登记，解释器一致"
            if matches else
            f"{info['label']} 已登记，但解释器不一致——"
            f"当前是 {info['entry'].get('command')!r}，"
            f"应为 {info['expected_command']!r}"
        ),
        data=info,
    ))


# --------------------------------------------------------------------------- #
# 端到端探针
# --------------------------------------------------------------------------- #


async def _probe(reg: Any) -> dict[str, Any]:
    """对零依赖 agent 发一个带随机 token 的任务，验证事件流真的回来了。"""
    from .models import Message

    def healthy(r: Any) -> bool:
        """健康状态在 ``rec.health`` 字典里，不是 ``rec.status``。"""
        return (getattr(r, "health", None) or {}).get("status") == "healthy"

    rec = None
    for aid in _PROBE_PREFERRED:
        try:
            if reg.has(aid):
                rec = reg.get(aid)
                break
        except Exception:  # noqa: BLE001 - 探测环境异常不该拖垮体检
            continue
    if rec is None:
        for r in reg.list_records():
            if healthy(r):
                rec = r
                break
    if rec is None:
        return {"ok": False, "agent": None,
                "detail": "没有 healthy 的 agent 可做探针"}

    token = "a2a-probe-" + secrets.token_hex(4)
    msg = Message.user(f"连通性自检，请原样回显这个字符串：{token}")
    text = ""
    try:
        task = reg.new_task(rec.id, msg)
        async for event in reg.execute(rec, task, msg):
            if event.kind == "artifact-update" and event.artifact.name == "output":
                parts = getattr(event.artifact, "parts", None) or []
                text = "".join(
                    getattr(p, "text", "") or "" for p in parts
                ) or text
    except Exception as exc:  # noqa: BLE001 - 探针失败要如实回报，不能拖垮体检
        return {"ok": False, "agent": getattr(rec, "id", None), "token": token,
                "detail": f"{type(exc).__name__}: {exc}"}

    state = getattr(getattr(task, "status", None), "state", None)
    state = getattr(state, "value", state)
    return {
        "ok": token in text and state == "completed",
        "agent": rec.id,
        "token": token,
        "state": state,
        "echoed": token in text,
        "detail": (f"{rec.id} 往返正常（state={state}）"
                   if token in text and state == "completed"
                   else f"{rec.id} 未回显 token（state={state}）"),
    }


async def run(
    settings: Any,
    *,
    probe: bool = True,
    host: str = "workbuddy",
    project_root: Optional[Path] = None,
) -> Report:
    """跑完整套体检。``probe=False`` 跳过端到端往返（纯静态检查）。

    ``project_root`` 只影响「项目级 MCP host（claude / cursor）」的配置定位，
    测试用它把检查限制在临时目录里。
    """
    from .registry import bootstrap

    report = Report()
    _check_env(report, settings)

    reg = bootstrap(settings)
    try:
        # force=True：体检要的是**此刻**的真实状态，不能命中 TTL 缓存，
        # 否则刚补完 key 也会显示「尚未探测」。
        await reg.check_health(force=True)
    except Exception:  # noqa: BLE001 - 健康检查炸了也要继续出报告
        pass
    _check_agents(report, list(reg.list_records()))
    _check_social(report, settings)
    _check_mcp(report, host, project_root)

    if probe:
        result = await _probe(reg)
        report.probe = result
        _add(report, Check(
            key="probe",
            title="端到端往返",
            ok=bool(result.get("ok")),
            detail=result.get("detail", ""),
            data=result,
        ))
    return report


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

_ICON = {True: "✓", False: "✗"}


def render(report: Report, *, color: bool = True) -> str:
    """人类可读报告。可选项目不合格时标 ``!`` 而不是 ``✗``——
    它不阻塞「能用」，不该跟真事故混在一起。"""

    def tint(s: str, ok: bool, optional: bool) -> str:
        if not color:
            return s
        if ok:
            return f"\033[32m{s}\033[0m"
        return f"\033[33m{s}\033[0m" if optional else f"\033[31m{s}\033[0m"

    lines = ["A2A Hub 就绪体检", "─" * 46]
    for chk in report.checks:
        mark = _ICON[chk.ok] if (chk.ok or not chk.optional) else "!"
        tail = "（可选）" if chk.optional else ""
        lines.append(
            f"{tint(mark, chk.ok, chk.optional)} {chk.title}{tail}"
            + (f"  {chk.detail}" if chk.detail else "")
        )

    if report.agents:
        lines += ["", "可被调用的 agent："]
        for a in report.agents:
            mark = "●" if a["ready"] else "○"
            note = "" if a["ready"] else f"  {a['status']} · {a['detail']}"
            lines.append(f"  {mark} {a['id']:<14}{a['name']}{note}")

    if report.probe:
        p = report.probe
        lines += ["", f"探针：{p.get('detail', '')}"]

    lines += ["", "─" * 46]
    if report.ok:
        lines.append(tint("结论：就绪，可以用。", True, False))
    else:
        lines.append(tint("结论：还不能用，先修上面的必需项。", False, False))
    if report.next_steps:
        lines.append("")
        lines.append("下一步：")
        lines += [f"  {i}. {s}" for i, s in enumerate(report.next_steps, 1)]
    return "\n".join(lines)


__all__ = ["Check", "Report", "render", "run"]
