"""接入工具箱 —— 把「接入包」从**说明文本**变成**可执行动作**。

## 为什么单独一个模块

`capabilities.py` 被三个出口共用（CLI `capabilities` / HTTP `GET /capabilities`
/ RPC `hub/capabilities`），它必须是**纯数据 + 纯渲染、零副作用**——
否则一个只读的 HTTP 端点会顺手具备写文件的能力。

而「拿来就能用」的最后一步是**有副作用**的：把 MCP 配置真正合并进 host 的
配置文件。所以把副作用收在这里，由 CLI 独占调用。

## 一个 host 三件事

| 动作 | 干什么 | 为什么需要 |
| --- | --- | --- |
| `resolve_path` | 定位 host 的 MCP 配置文件 | 每个 host 位置不同，写错了等于没装 |
| `register` | 幂等合并 + 写前备份 | 「装」 |
| `inspect` | 装了没 / 解释器路径对不对 | 「验」——最常见的事故是拿裸 `python` 拉起，进程内 import 失败 |

## 纪律

- **幂等**：重复 `register` 只改自己那条，不动别人、不堆叠。
- **保留未知字段**：只更新 `command`/`args`，host 自己加的 `disabled`、
  `env`、`timeout` 一律原样留着——那是用户的配置，不是我们的。
- **写前备份**：文件已存在且内容将发生变化时，先落一份 `.bak-<时间戳>`。
- **只认 JSON host**：Codex 用 TOML（`~/.codex/config.toml`），Python 标准库
  只有只读的 `tomllib`，硬拼 TOML 会破坏用户文件，所以它的 `writable=False`，
  只给片段、让人自己粘。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: 写入 MCP 配置时用的服务名（要跟 `capabilities.mcp_server_config` 一致，
#: 否则同一台机器上会出现两条指向同一 server 的条目）
DEFAULT_NAME = "a2a-hub"


@dataclass(frozen=True)
class McpHost:
    """一个能挂 MCP server 的宿主。"""

    key: str
    label: str
    #: 配置文件位置。``~`` 开头按用户主目录展开；否则相对项目根。
    path: str
    #: 该 host 的 MCP 配置是哪种格式（目前只自动写 json）
    fmt: str = "json"
    #: 落盘时用的顶层 key
    root_key: str = "mcpServers"
    #: 能不能自动写（False = 只能给片段）
    writable: bool = True
    note: str = ""


#: 已知宿主。**新增生态在这里加一行**，`register` / `inspect` 自动支持。
HOSTS: tuple[McpHost, ...] = (
    McpHost(
        key="workbuddy",
        label="WorkBuddy（本机）",
        path="~/.workbuddy/mcp.json",
        note="写完要到连接器管理页点一次「信任」才生效。",
    ),
    McpHost(
        key="claude",
        label="Claude Code（项目级）",
        path=".mcp.json",
        note="放在项目根，Claude Code 在该目录下启动即可见；也可放 ~/.claude.json。",
    ),
    McpHost(
        key="cursor",
        label="Cursor（项目级）",
        path=".cursor/mcp.json",
        note="Cursor 在设置 → MCP 里也能看到同名条目。",
    ),
    McpHost(
        key="codex",
        label="Codex（全局）",
        path="~/.codex/config.toml",
        fmt="toml",
        root_key="mcp_servers",
        writable=False,
        note="TOML 格式，自动写有破坏风险，请按片段手工合并。",
    ),
)

#: 默认宿主（本机把 Hub 用起来的主通道）
DEFAULT_HOST = "workbuddy"


def by_key(key: str) -> Optional[McpHost]:
    """按 key 取宿主；取不到返回 ``None``。"""
    for h in HOSTS:
        if h.key == key:
            return h
    return None


def resolve_path(host: McpHost, *, project_root: Optional[Path] = None) -> Path:
    """把 ``McpHost.path`` 展开成绝对路径。

    ``~`` 开头的按主目录展开（WorkBuddy/Codex 是全局配置），
    其余按项目根拼（Claude Code/Cursor 的项目级配置）。
    """
    raw = host.path
    if raw.startswith("~"):
        return Path(raw).expanduser()
    root = project_root or Path(__file__).resolve().parents[1]
    return (root / raw).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    """读配置；不存在或坏掉都退回空 dict（坏文件交给调用方决定要不要覆盖）。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_entry(*, python: str, server_path: str) -> dict[str, Any]:
    """生成一条 MCP server 条目（与 `capabilities.mcp_server_config` 同源）。"""
    return {"command": python, "args": [server_path]}


def inspect(
    host_key: str,
    *,
    python: str = "",
    server_path: str = "",
    project_root: Optional[Path] = None,
    name: str = DEFAULT_NAME,
) -> dict[str, Any]:
    """检查「装了没」。

    返回 ``registered``（有没有这条）、``command_matches``（解释器对不对）。
    后者是**最容易翻车**的地方：MCP server 进程内 import `a2a_hub`，
    必须由装了项目依赖的解释器拉起；换成裸 `python` 会静默失败。
    """
    from .capabilities import mcp_server_config

    host = by_key(host_key)
    if host is None:
        return {"host": host_key, "known": False, "registered": False}
    want = mcp_server_config(python=python, server_path=server_path, name=name)
    want_entry = want["mcpServers"][name]
    path = resolve_path(host, project_root=project_root)
    data = _load_json(path)
    entry = (data.get(host.root_key) or {}).get(name) if isinstance(
        data.get(host.root_key), dict
    ) else None
    return {
        "host": host.key,
        "label": host.label,
        "known": True,
        "path": str(path),
        "exists": path.exists(),
        "writable": host.writable,
        "registered": entry is not None,
        "entry": entry,
        "command_matches": bool(
            entry and str(entry.get("command", "")).lower()
            == str(want_entry["command"]).lower()
        ),
        "expected_command": want_entry["command"],
        "note": host.note,
    }


def register(
    host_key: str,
    *,
    python: str = "",
    server_path: str = "",
    project_root: Optional[Path] = None,
    name: str = DEFAULT_NAME,
) -> dict[str, Any]:
    """把 Hub 登记进 host 的 MCP 配置。**幂等 + 写前备份。**

    :returns: ``{ok, path, changed, created, backup, error}``
    """
    host = by_key(host_key)
    if host is None:
        return {"ok": False, "error": f"未知 host：{host_key}"}
    if not host.writable:
        return {
            "ok": False,
            "error": f"{host.label} 的配置是 {host.fmt.upper()} 格式，"
                     f"自动写有破坏风险，请按 attach 输出的片段手工合并。",
            "path": str(resolve_path(host, project_root=project_root)),
        }

    from .capabilities import mcp_server_config

    entry = mcp_server_config(python=python, server_path=server_path,
                              name=name)["mcpServers"][name]
    path = resolve_path(host, project_root=project_root)
    existed = path.exists()
    data = _load_json(path)
    servers = data.setdefault(host.root_key, {})
    if not isinstance(servers, dict):  # 用户把这里写成别的类型了
        return {"ok": False, "error": f"{path} 的 `{host.root_key}` 不是对象",
                "path": str(path)}

    old = servers.get(name)
    # **只动 command / args，保留 host 自己加的字段**（disabled / env / timeout…）
    merged = dict(old) if isinstance(old, dict) else {}
    merged["command"] = entry["command"]
    merged["args"] = entry["args"]
    changed = merged != old
    if changed:
        servers[name] = merged

    backup: Optional[str] = None
    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if existed:
                backup = str(path) + f".bak-{time.strftime('%Y%m%d%H%M%S')}"
                shutil.copy2(path, backup)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return {"ok": False, "error": f"写入 {path} 失败：{exc}",
                    "path": str(path)}

    return {
        "ok": True,
        "path": str(path),
        "changed": changed,
        "created": changed and not existed,
        "backup": backup,
        "note": host.note,
    }


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_NAME",
    "HOSTS",
    "McpHost",
    "build_entry",
    "by_key",
    "inspect",
    "register",
    "resolve_path",
]
