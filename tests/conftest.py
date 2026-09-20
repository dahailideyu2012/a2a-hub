"""pytest 公共夹具 —— 每个测试用独立的临时配置与全新注册中心，避免串味。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TEST_AGENTS = {
    "hub": {"name": "A2A Hub (test)"},
    "agents": [
        {
            "id": "echo-a",
            "name": "回显 A",
            "type": "echo",
            "description": "测试用回显 agent，擅长代码评审与写作",
            "priority": 1,
            "tags": ["test", "review"],
            "skills": [
                {
                    "id": "code-review",
                    "name": "代码评审",
                    "description": "审查代码质量与安全",
                    "tags": ["代码", "code", "评审", "review"],
                }
            ],
            "config": {"prefix": "[A]", "emit_data_part": True},
        },
        {
            "id": "echo-b",
            "name": "回显 B",
            "type": "echo",
            "description": "测试用回显 agent，擅长中文写作与翻译",
            "priority": 0,
            "tags": ["test", "writing"],
            "skills": [
                {
                    "id": "writing",
                    "name": "中文写作",
                    "description": "撰写报告与文案",
                    "tags": ["写作", "writing", "报告", "文案"],
                }
            ],
            "config": {"prefix": "[B]"},
        },
        {
            "id": "echo-fail",
            "name": "会失败的 Agent",
            "type": "echo",
            "description": "命中 fail_on 关键词时失败",
            "priority": -5,
            "auto_route": False,
            "skills": [{"id": "x", "name": "占位", "description": "", "tags": ["placeholder"]}],
            "config": {"prefix": "[F]", "fail_on": "BOOM"},
        },
        {
            "id": "static-card",
            "name": "固定应答",
            "type": "static",
            "description": "固定返回配置中的文本",
            "priority": -5,
            "auto_route": False,
            "skills": [{"id": "s", "name": "固定", "description": "", "tags": ["static"]}],
            "config": {"response": "固定应答内容"},
        },
    ],
}


@pytest.fixture
def agents_file(tmp_path: Path) -> Path:
    p = tmp_path / "agents.yaml"
    p.write_text(yaml.safe_dump(TEST_AGENTS, allow_unicode=True), encoding="utf-8")
    return p


@pytest.fixture
def settings(agents_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("A2A_AGENTS_FILE", str(agents_file))
    monkeypatch.setenv("A2A_STORE", "memory")
    monkeypatch.setenv("A2A_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("A2A_API_TOKEN", "")
    monkeypatch.delenv("A2A_REQUIRE_AUTH", raising=False)

    import a2a_hub.config as cfg
    import a2a_hub.registry as reg

    cfg._settings = None
    reg._registry = None
    s = cfg.Settings()
    yield s
    cfg._settings = None
    reg._registry = None


@pytest.fixture
def registry(settings):
    import a2a_hub.registry as reg

    r = reg.bootstrap(settings)
    yield r


@pytest.fixture
def orchestrator(registry):
    from a2a_hub.orchestrator import Orchestrator

    return Orchestrator(registry, registry.bus)


@pytest.fixture
def dispatcher(registry, orchestrator):
    from a2a_hub.rpc import JsonRpcDispatcher

    return JsonRpcDispatcher(registry, orchestrator)


@pytest.fixture
def client(settings):
    """全新的 FastAPI TestClient（重建 module 级 hub，隔离测试）。"""
    from fastapi.testclient import TestClient

    import a2a_hub.server as srv

    srv.hub = srv.Hub()
    with TestClient(srv.app) as c:
        yield c
