"""Hub 配置 —— 环境变量 + agents.yaml 双来源。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """递归展开 ${VAR} / ${VAR:-default} 占位符。

    递归处理是为了支持任意嵌套的 dict / list 配置。
    """
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            return os.environ.get(name) or (default or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = Field(default="0.0.0.0", alias="A2A_HOST")
    port: int = Field(default=8080, alias="A2A_PORT")
    public_url: str = Field(default="http://localhost:8080", alias="A2A_PUBLIC_URL")
    hub_name: str = Field(default="A2A Hub", alias="A2A_HUB_NAME")
    store: str = Field(default="memory", alias="A2A_STORE")
    db_path: str = Field(default="./data/a2a_hub.db", alias="A2A_DB_PATH")
    task_timeout: int = Field(default=300, alias="A2A_TASK_TIMEOUT")
    log_level: str = Field(default="INFO", alias="A2A_LOG_LEVEL")
    api_token: str = Field(default="", alias="A2A_API_TOKEN")
    require_auth: bool = Field(default=False, alias="A2A_REQUIRE_AUTH")
    agents_file: str = Field(default="./config/agents.yaml", alias="A2A_AGENTS_FILE")
    # --- 社交层（见 docs/identity-and-binding.md）---
    #: 成员声明。**文件不存在 = 门禁自动关闭，行为与 v0.3.0 一致。**
    members_file: str = Field(default="./config/members.yaml", alias="A2A_MEMBERS_FILE")
    #: 关系与好友。运行时状态，由程序写，不要手编。
    relations_file: str = Field(
        default="./data/relations.json", alias="A2A_RELATIONS_FILE"
    )
    #: off = 关闭门禁；soft = 非好友仅能 chat；strict = 非好友一律拒绝。
    social_mode: str = Field(default="strict", alias="A2A_SOCIAL_MODE")
    #: 社交简报：执行前把「你是谁/好友列表/缺口信号协议」注入 prompt，
    #: 让 prompt 型 agent（WorkBuddy/Codex/Claude Code…）零改动获得社交感知。
    #: 仅在社交层启用时生效；不想要就在 members.yaml 之外设 false。
    social_briefing: bool = Field(default=True, alias="A2A_SOCIAL_BRIEFING")
    # --- 自主交友（§6）---
    #: 社交巡航总开关。**默认关**：开了才会有后台的网络请求/申请动作。
    autonomy_enabled: bool = Field(default=False, alias="A2A_AUTONOMY_ENABLED")
    #: 巡航轮询间隔（秒）。
    autonomy_interval: float = Field(default=300.0, alias="A2A_AUTONOMY_INTERVAL")
    #: 每轮最多发起的自主申请数。
    autonomy_per_round: int = Field(default=2, alias="A2A_AUTONOMY_PER_ROUND")
    #: 每天最多发起的自主申请数（巡航侧硬预算）。
    autonomy_daily: int = Field(default=6, alias="A2A_AUTONOMY_DAILY")

    def agents_path(self) -> Path:
        p = Path(self.agents_file)
        return p if p.is_absolute() else (ROOT / p).resolve()

    def members_path(self) -> Path:
        p = Path(self.members_file)
        return p if p.is_absolute() else (ROOT / p).resolve()

    def relations_path(self) -> Path:
        p = Path(self.relations_file)
        return p if p.is_absolute() else (ROOT / p).resolve()


class AgentSpec(BaseModel):
    """agents.yaml 中的单条 agent 声明。"""

    id: str
    name: str
    type: str
    description: str = ""
    enabled: bool = True
    transport: str = "LOCAL"
    url: Optional[str] = None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # 协同编排权重：数值越高越优先被 delegate 选中
    priority: int = 0
    # 是否允许被自动路由（false 时只能显式点名调用）
    auto_route: bool = True


class HubConfig(BaseModel):
    hub: dict[str, Any] = Field(default_factory=dict)
    agents: list[AgentSpec] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "HubConfig":
        if not path.exists():
            return cls(agents=[])
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = expand_env(raw)
        return cls(**raw)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
