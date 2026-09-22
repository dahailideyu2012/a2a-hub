"""社交层一键启用（bootstrap）。

社交门禁的开关是「``config/members.yaml`` 存不存在」——文件不在，门禁整体
关闭，行为与 v0.3.0 逐字节一致。这个设计对老用户友好，但对新用户是一堵墙：
得先找到 ``members.example.yaml``、手工复制、再重启服务，中间还要踩
「示例里写了 ``tokens: ["${A2A_TOKEN_XXX}"]`` 而环境变量没设」这类坑。

本模块把这件事收成一个函数调用：

    python -m a2a_hub.cli social init        # 命令行
    a2a_social_init                          # MCP / WorkBuddy 里直接调

三条纪律：

1. **幂等且绝不覆盖已有文件。** 用户手改过的 ``members.yaml`` 是资产，
   一键启用不能把它冲掉；检测到已存在就原样返回 ``created=False``。
2. **绝不写明文 token，也不写未展开的 ``${VAR}`` 占位符。**
   一个 token 都不配时按本地开发模式处理（一律视为默认人类），这是最不容易
   把部署者锁在门外的状态；要鉴权请自己往生成的文件里加。
3. **显式给每个 agent 声明 ``owner``。** 不声明时单人部署虽然会自动回落到
   那个人，但多人类部署下 ``effective_owner()`` 拒绝猜测——一键生成必须落在
   「一定能用」的那一边。

生成结果只保证「开箱可用」，不保证「生产安全」：它把所有本地 agent 都挂在
同一个人名下并设为圈层可见。要细粒度控制，照着 ``members.example.yaml`` 改。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: 一键生成时默认人类的成员 id（``user`` 这个保留字会映射到他）
DEFAULT_OWNER_ID = "human:default"
#: 一键生成时默认人类的展示名
DEFAULT_OWNER_NAME = "我（Hub 部署者）"

_HEADER = """\
# ============================================================================
# A2A Hub —— 成员表（由 `social init` 自动生成）
#
# 这份文件存在 = 社交门禁整体启用。删掉它即可回到「无门禁」的 v0.3.0 行为。
#
# 生成时做了三件保守的选择，方便你在此基础上改：
#   1. 不写任何 token —— 一个 token 都不配时按本地开发模式处理，
#      请求一律视为 human:default，不会把自己锁在门外。要鉴权就自己加：
#        tokens: ["${A2A_TOKEN_SEAFISH}"]     # 支持环境变量占位符
#   2. 所有本地 agent 都归到你（human:default）名下 —— 你对它们天然全权，
#      可以立刻 delegate，不用先加好友。
#   3. 每个 agent 都是 circle 可见 —— 好友的好友能发现它，陌生人搜不到。
#
# 想看完整能力（自主交友、权限天花板 max_scopes、服务账号等）参考
# config/members.example.yaml。完整说明见 docs/social-guide.md。
#
# 相关环境变量：
#   A2A_MEMBERS_FILE    默认 ./config/members.yaml
#   A2A_RELATIONS_FILE  默认 ./data/relations.json（运行时状态，程序写，别手编）
#   A2A_SOCIAL_MODE     off | soft | strict（默认 strict）
# ============================================================================
"""


def load_agent_declarations(agents_path: Path | str | None) -> list[dict[str, str]]:
    """从 ``agents.yaml`` 读出 agent 声明，只取 bootstrap 用得上的三个字段。

    读不到 / 格式不对都返回空列表——**没声明 agent 不该让一键启用失败**，
    只是生成的通讯录里少几个人而已（图还会自己补 registry 里的 agent 节点）。
    """
    if not agents_path:
        return []
    p = Path(agents_path)
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("读取 %s 失败，跳过 agent 声明：%s", p, exc)
        return []
    out: list[dict[str, str]] = []
    for item in raw.get("agents") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append(
            {
                "id": str(item["id"]),
                "name": str(item.get("name") or item["id"]),
                "bio": str(item.get("description") or "").strip(),
            }
        )
    return out


def render_members_yaml(
    agents: list[dict[str, str]],
    *,
    owner_id: str = DEFAULT_OWNER_ID,
    owner_name: str = DEFAULT_OWNER_NAME,
) -> str:
    """渲染 ``members.yaml`` 文本。

    用 ``yaml.safe_dump`` 而不是手拼字符串：agent 的 bio 是从 ``agents.yaml``
    里抄来的自由文本，**可能带引号、冒号或换行**，手拼会生成坏 YAML。
    """
    members: list[dict[str, Any]] = [
        {
            "id": owner_id,
            "name": owner_name,
            "kind": "human",
            "bio": "Hub 的部署者，负责编排与验收",
            "discoverable": "circle",
        }
    ]
    for a in agents:
        entry: dict[str, Any] = {
            "id": f"agent:{a['id']}",
            "name": a["name"],
            "kind": "agent",
            "owner": owner_id,
            "discoverable": "circle",
        }
        if a.get("bio"):
            entry["bio"] = a["bio"]
        members.append(entry)

    body = yaml.safe_dump(
        {"members": members},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    return _HEADER + "\n" + body


def bootstrap_members(
    *,
    members_path: Path | str,
    agents_path: Path | str | None = None,
    owner_id: str = DEFAULT_OWNER_ID,
    owner_name: str = DEFAULT_OWNER_NAME,
    force: bool = False,
) -> dict[str, Any]:
    """确保 ``members.yaml`` 存在。已存在则**不动**，除非 ``force=True``。

    返回 ``{"created": bool, "path": str, "members": [...], "reason": str}``，
    ``members`` 是生成/已存在的成员 id 列表，方便调用方直接回显给人看。
    """
    path = Path(members_path)
    existed = path.exists()

    if existed and not force:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            ids = [m.get("id") for m in (raw.get("members") or []) if isinstance(m, dict)]
        except (OSError, yaml.YAMLError):
            ids = []
        return {
            "created": False,
            "path": str(path),
            "members": [i for i in ids if i],
            "reason": "members.yaml 已存在，未改动",
        }

    agents = load_agent_declarations(agents_path)
    text = render_members_yaml(agents, owner_id=owner_id, owner_name=owner_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再 replace：半截 YAML 会让整个社交层起不来
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("写入 %s 失败：%s", path, exc)
        return {
            "created": False,
            "path": str(path),
            "members": [],
            "reason": f"写入失败：{exc}",
        }

    ids = [owner_id] + [f"agent:{a['id']}" for a in agents]
    return {
        "created": True,
        "path": str(path),
        "members": ids,
        "reason": f"已生成，共 {len(ids)} 个成员（1 个人类 + {len(agents)} 个 agent）",
    }
