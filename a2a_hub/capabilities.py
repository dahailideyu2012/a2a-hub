"""能力清单 —— 「各 agent 怎么套用 Hub 的能力」的单一事实来源。

## 为什么需要它

Hub 的能力有三种通道（CLI / HTTP·JSON-RPC / MCP），但在此之前**没有任何一处
把它们并排列出来**：想接一个新 agent，得去翻 `cli.py` 和 `server.py` 才知道
有哪些能力、怎么调、要不要授权；而 agent 自己（尤其 prompt 型）更是完全不知道
「我还能调用什么」。

更糟的是三处各写一遍必然漂移——今天给 `social/grant` 加了 `--as`，明天忘了
同步到 MCP 工具说明，就会出现「文档说能调、实际调不通」。

所以把清单收在这里：**一条能力登记四种接法**，渲染器按调用方的「手」产出对应
形态。加能力时改这一处，三个出口（CLI `capabilities` / HTTP `GET /capabilities`
/ MCP `tools/list`）自动跟上。

## 「手」决定套用方式

| agent 的形态 | 它有什么手 | 套用方式 |
| --- | --- | --- |
| WorkBuddy / Claude Code / Codex / Cursor | 支持 MCP | 加一段 MCP server 配置 |
| 任何能跑 shell 的 agent / 脚本 | 命令行 | 把 CLI 用法写进它的说明文件 |
| 云端 agent（千问 / 扣子 / Kimi…） | HTTP | 给地址与鉴权方式 |
| 纯 prompt 型（不会自己发请求） | 只有上下文 | 把能力写进它的系统提示词 |

`attach` 子命令就是照这张表生成「可直接粘贴的套用包」。

## 纪律

- **别在这里放实现细节**（端口默认值、鉴权算法），那些是会变的；这里只放
  「叫什么、干什么、怎么调」。
- **写操作要标 `write=True`**：套用方据此知道要先征得同意。
- **`scope` 是社交权限门槛**，不是鉴权方式：`delegate` 那一列表示「对方得先
  授予你 delegate 才调得通」。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

#: 写在接入产物里的标记块，便于幂等更新（重复 attach 不会堆叠）
MARK_BEGIN = "<!-- A2A-HUB:BEGIN 由 `python run.py attach` 生成，可重复执行 -->"
MARK_END = "<!-- A2A-HUB:END -->"


@dataclass(frozen=True)
class Capability:
    """一条对外能力在三种通道上的接法。

    字段留空表示该通道不提供这条能力（例如 `social init` 没有 HTTP 端点）。
    """

    id: str
    summary: str
    group: str = "core"
    #: CLI 用法（相对 `python run.py` 的部分）
    cli: str = ""
    #: JSON-RPC method
    rpc: str = ""
    #: HTTP 方法 + 路径
    http: str = ""
    #: MCP 工具名
    mcp: str = ""
    #: 需要的社交权限档位（空 = 无门槛）
    scope: str = ""
    #: 写操作（套用方应先确认再调）
    write: bool = False
    #: 给 agent 看的一句话补充（渲染 prompt 时用，比 summary 更口语）
    hint: str = ""


#: 对外能力清单。**改这里，三个出口自动跟上。**
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="agents",
        summary="列出所有 agent：会什么、现在能不能用",
        cli="agents",
        rpc="agents/list",
        http="GET /agents",
        mcp="a2a_agents",
        hint="派活前先确认「谁在线、会什么」，别凭印象点人。",
    ),
    Capability(
        id="ask",
        summary="派一个任务给某个 agent（不指定则按能力自动路由）",
        cli='ask "<任务>" [--agent <id>] [--as <成员>]',
        rpc="message/send",
        http="POST /tasks",
        mcp="a2a_delegate",
        scope="delegate",
        hint="不写 --agent 时 Hub 会按 skills 打分自动选人。",
    ),
    Capability(
        id="collab",
        summary="多 agent 协同（delegate / broadcast / pipeline / roundtable）",
        cli='collab <mode> "<任务>" [--agents a,b] [--rounds N]',
        rpc="collab/run",
        http="POST /collab",
        mcp="a2a_collab",
        scope="delegate",
    ),
    Capability(
        id="task",
        summary="查任务状态与产物（长任务可先派后查）",
        rpc="tasks/get",
        http="GET /tasks/{task_id}",
        mcp="a2a_task",
        hint="派活返回的 taskId 用它复查；跑得久就轮询它，别干等。",
    ),
    Capability(
        id="chat",
        summary="像微信一样和 agent 聊天；可建群拉多人（消息与执行解耦）",
        cli='im chat <agent> "<消息>"  /  im group --members a,b "<问题>"',
        http="POST /im/conversations/{conv_id}/messages",
        hint="群里 @某人只叫醒他，@所有人全员，没人被 @ 按能力挑一个。",
    ),
    Capability(
        id="social-read",
        summary="看社交网络：我的名片 / 好友 / 找新伙伴 / 申请箱 / 待办",
        cli="social me | friends | find | discover --need <能力> | inbox | pending",
        rpc="social/me",
        http="GET /social/me",
        mcp="a2a_social",
    ),
    Capability(
        id="social-write",
        summary="改社交关系：申请 / 同意 / 授权 / 拉黑 / 报告能力缺口",
        cli="social add|accept|grant|revoke|block|need …",
        rpc="social/request",
        http="POST /social/requests",
        mcp="a2a_social_act",
        write=True,
        hint="grant 里给了 delegate 才等于允许对方派活给自己。",
    ),
    Capability(
        id="social-init",
        summary="一键启用社交层（生成 members.yaml 并热启用，幂等）",
        cli="social init",
        mcp="a2a_social_init",
        write=True,
    ),
    Capability(
        id="capabilities",
        summary="这份能力清单本身（机器可读）",
        cli="capabilities [--json]",
        rpc="hub/capabilities",
        http="GET /capabilities",
    ),
)


def by_id(cap_id: str) -> Optional[Capability]:
    """按 id 取能力；取不到返回 ``None``（调用方自己决定怎么报错）。"""
    for c in CAPABILITIES:
        if c.id == cap_id:
            return c
    return None


def groups() -> dict[str, list[Capability]]:
    """按 group 分组，保持声明顺序。"""
    out: dict[str, list[Capability]] = {}
    for c in CAPABILITIES:
        out.setdefault(c.group, []).append(c)
    return out


# --------------------------------------------------------------------------- #
# 渲染：同一份清单，按调用方的「手」产出不同形态
# --------------------------------------------------------------------------- #


def manifest(*, base_url: str = "", include_meta: bool = True) -> dict[str, Any]:
    """机器可读清单（HTTP `GET /capabilities` / CLI `--json` 用）。"""
    out: dict[str, Any] = {
        "capabilities": [asdict(c) for c in CAPABILITIES],
    }
    if include_meta:
        out["meta"] = {
            "transports": {
                "cli": "python run.py <cmd>",
                "http": (base_url.rstrip("/") or "<hub-base-url>") + "/...",
                # A2A 的 JSON-RPC 端点就是根路径，别写成 /rpc（那是 404）
                "rpc": "POST <hub-base-url>/  (JSON-RPC 2.0)",
                "mcp": "把 mcp_server.py 注册进 MCP host",
            },
            "scope_note": "标了 scope 的能力需要对方先授予该权限档位",
            "confirm_note": "write=true 的是写操作，调用前先征得同意",
        }
    return out


def render_table(items: Optional[tuple[Capability, ...]] = None) -> str:
    """终端表格：一眼看清「有什么能力、CLI / HTTP / MCP 分别怎么调」。"""
    items = items or CAPABILITIES
    lines: list[str] = []
    for grp, caps in groups().items():
        if items is not CAPABILITIES and not any(c in items for c in caps):
            continue
        lines.append(f"\n【{grp}】")
        for c in caps:
            if items is not CAPABILITIES and c not in items:
                continue
            gate = f"  ⟵ 需 {c.scope}" if c.scope else ""
            w = "（写操作）" if c.write else ""
            lines.append(f"  {c.id:<13} {c.summary}{w}{gate}")
            if c.cli:
                lines.append(f"      CLI : python run.py {c.cli}")
            if c.rpc:
                lines.append(f"      RPC : {c.rpc}")
            if c.http:
                lines.append(f"      HTTP: {c.http}")
            if c.mcp:
                lines.append(f"      MCP : {c.mcp}")
    return "\n".join(lines)


def render_prompt(
    *,
    base_url: str = "",
    identity: str = "",
    include: Optional[tuple[Capability, ...]] = None,
    max_items: int = 8,
) -> str:
    """给 **prompt 型 agent** 的一段能力说明（写进它的系统提示词 / AGENTS.md）。

    刻意压缩到十来行：这段要常驻上下文窗口，啰嗦就是长期成本。
    """
    items = (include or CAPABILITIES)[:max_items]
    head = "[A2A Hub 协作能力]"
    who = f"你（{identity}）" if identity else "你"
    lines = [
        head,
        f"{who}可以调用本机的 A2A Hub 与其他 agent 协作。可用能力：",
    ]
    for c in items:
        # prompt 里只给 CLI（能跑命令的 agent）+ 一句话，其余交给 capabilities 命令
        how = f"`python run.py {c.cli.split('  /')[0].strip()}`" if c.cli else f"`{c.rpc or c.http}`"
        gate = f"（需对方授予 {c.scope}）" if c.scope else ""
        lines.append(f"- {c.summary} → {how}{gate}")
    if base_url:
        lines.append(f"（HTTP 方式：{base_url.rstrip('/')}，鉴权用 Bearer token）")
    lines += [
        "先跑 `python run.py capabilities` 可取完整清单；不确定派给谁就先跑 `agents`。",
        "纪律：不要把 Hub 的内部地址、token 或成员关系写进对外产出。",
        "[说明结束]",
    ]
    return "\n".join(lines)


def mcp_server_config(
    *,
    python: str = "",
    server_path: str = "",
    name: str = "a2a-hub",
) -> dict[str, Any]:
    """MCP 接入片段（直接合并进 host 的 mcp.json）。

    路径默认取**当前解释器与当前仓库**——MCP server 是进程内 import
    `a2a_hub` 的，所以必须由装了项目依赖的解释器拉起，不能想当然写 `python`。
    """
    py = python or sys.executable
    srv = server_path or str(Path(__file__).resolve().parents[1] / "mcp_server.py")
    return {"mcpServers": {name: {"command": py, "args": [srv]}}}


def render_attach(
    transport: str,
    *,
    identity: str = "",
    base_url: str = "",
    python: str = "",
    server_path: str = "",
) -> str:
    """生成「可直接粘贴」的接入产物。``transport`` 见下表：

    - ``mcp``    → mcp.json 片段（WorkBuddy / Claude Code / Codex / Cursor）
    - ``cli``    → 命令行速查（任何能跑 shell 的 agent）
    - ``http``   → 地址与鉴权要点（云端 agent）
    - ``prompt`` → 系统提示词片段（纯 prompt 型 agent）
    """
    t = (transport or "auto").lower()
    if t == "mcp":
        cfg = json.dumps(mcp_server_config(python=python, server_path=server_path),
                         ensure_ascii=False, indent=2)
        return (
            "把下面这段合并进 MCP host 的配置（WorkBuddy 是 ~/.workbuddy/mcp.json，\n"
            "Claude Code / Codex 见各自文档），然后**在连接器管理页点一次「信任」**：\n\n"
            f"{cfg}\n\n"
            "注意：解释器必须是装了本项目依赖的那个（上面已填好当前解释器路径）。\n"
            "注册后可用的工具：" + "、".join(c.mcp for c in CAPABILITIES if c.mcp)
        )
    if t == "cli":
        return (
            "把这个路径告诉 agent（能跑 shell 就行），它就能调用 Hub：\n\n"
            "    python run.py capabilities        # 先让它自己看有什么能力\n"
            + render_table()
        )
    if t == "http":
        base = (base_url or "http://<hub-host>:8080").rstrip("/")
        return (
            f"HTTP 接入（云端 agent / 任何会发请求的进程）：\n\n"
            f"  基地址   {base}\n"
            f"  能力清单 {base}/capabilities        （公开，无需鉴权）\n"
            f"  JSON-RPC POST {base}/              （A2A 标准入口，就是根路径）\n"
            f"  派活     POST {base}/tasks       (JSON-RPC: message/send)\n"
            f"  协同     POST {base}/collab\n"
            f"  社交     GET  {base}/social/me\n"
            f"  鉴权     Authorization: Bearer <token>（成员表里配 tokens 后必需）\n"
            f"  长任务   GET  {base}/tasks/{{task_id}}/events  (SSE 流式)\n\n"
            "注意：`A2A_REQUIRE_AUTH=true` 或成员表里配了 token 时，没带 token 会被拒。"
        )
    # prompt
    return (
        "把下面这段放进该 agent 的系统提示词（或 CLAUDE.md / AGENTS.md）：\n\n"
        + render_prompt(base_url=base_url, identity=identity)
    )


__all__ = [
    "CAPABILITIES",
    "MARK_BEGIN",
    "MARK_END",
    "Capability",
    "by_id",
    "groups",
    "manifest",
    "mcp_server_config",
    "render_attach",
    "render_prompt",
    "render_table",
]
