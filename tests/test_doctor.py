"""就绪体检的回归 —— 守「验」这一步，以及它的诚实性。

`doctor` 的全部价值在于**结论可信**，所以测试盯的不是「输出好看」，而是：

1. **必需项与可选项不能混为一谈**：社交层没开、MCP 没登记都只是可选增强，
   把它们算成红的，会让一个健康的部署看着像坏了（狼来了）。
2. **探针要真往返**：不是「配置里有这一行」，而是事件流真的从 adapter 回来了，
   且回显内容与随机 token 对得上。
3. **失败要给出路**：每条不过的必需项都得配一句「下一步怎么办」，
   否则报告只是一份判决书，不是工具。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from a2a_hub import attach_kit, doctor
from a2a_hub.doctor import Check, Report

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeSettings:
    """只实现体检用到的那两个路径方法，免得为了造一个坏路径去改环境变量。"""

    def __init__(self, agents: str, members: str, mode: str = "strict") -> None:
        self._agents = agents
        self._members = members
        self.social_mode = mode

    def agents_path(self) -> str:
        return self._agents

    def members_path(self) -> str:
        return self._members


# --------------------------------------------------------------------------- #
# Report 的判定语义
# --------------------------------------------------------------------------- #


class TestReportSemantics:
    def test_all_pass_is_ok(self):
        r = Report(checks=[Check("a", "A", ok=True), Check("b", "B", ok=True)])
        assert r.ok is True

    def test_required_failure_blocks(self):
        r = Report(checks=[Check("a", "A", ok=True), Check("b", "B", ok=False)])
        assert r.ok is False

    def test_optional_failure_does_not_block(self):
        """社交层/MCP 是可选增强——没开不该把部署判成「不能用」。"""
        r = Report(checks=[
            Check("a", "A", ok=True),
            Check("social", "社交门禁", ok=False, optional=True),
            Check("mcp", "MCP 登记", ok=False, optional=True),
        ])
        assert r.ok is True

    def test_empty_report_is_ok(self):
        assert Report().ok is True

    def test_to_dict_is_json_serializable(self):
        r = Report(checks=[Check("a", "A", ok=True, data={"x": 1})],
                   agents=[{"id": "echo"}], probe={"ok": True})
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        assert json.loads(blob)["checks"][0]["key"] == "a"


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #


class TestChecks:
    def test_env_passes_on_this_interpreter(self, tmp_path: Path):
        r = Report()
        doctor._check_env(r, _FakeSettings(str(ROOT / "config" / "agents.yaml"),
                                           str(tmp_path / "members.yaml")))
        assert all(c.ok for c in r.checks)

    def test_missing_registry_is_a_required_failure(self, tmp_path: Path):
        r = Report()
        doctor._check_env(r, _FakeSettings(str(tmp_path / "nope.yaml"),
                                           str(tmp_path / "members.yaml")))
        assert r.ok is False
        assert any("agents.yaml" in s for s in r.next_steps)

    def test_agents_split_ready_from_blocked(self):
        class Rec:
            def __init__(self, aid, st, detail=""):
                self.id, self.name, self.type = aid, aid.upper(), "openai_compat"
                self.health = {"status": st, "detail": detail}

        r = Report()
        doctor._check_agents(r, [Rec("echo", "healthy"), Rec("qwen", "unavailable", "缺少 api_key")])
        chk = r.checks[0]
        assert chk.ok is True  # 有一个能用就算过
        assert chk.data["ready"] == ["echo"]
        assert chk.data["blocked"] == ["qwen"]

    def test_all_blocked_fails_and_hints(self):
        class Rec:
            id, name, type = "x", "X", "openai_compat"
            health = {"status": "unavailable", "detail": "缺少 api_key"}

        r = Report()
        doctor._check_agents(r, [Rec()])
        assert r.ok is False and r.next_steps

    def test_social_absent_is_optional_failure(self, tmp_path: Path):
        r = Report()
        doctor._check_social(r, _FakeSettings("", str(tmp_path / "members.yaml")))
        assert r.checks[0].ok is False and r.checks[0].optional is True
        assert r.ok is True
        assert any("social init" in s for s in r.next_steps)

    def test_social_present_is_ok(self, tmp_path: Path):
        m = tmp_path / "members.yaml"
        m.write_text("members: []\n", encoding="utf-8")
        r = Report()
        doctor._check_social(r, _FakeSettings("", str(m)))
        assert r.checks[0].ok is True and "strict" in r.checks[0].detail

    def test_mcp_not_registered_is_optional(self, tmp_path: Path):
        r = Report()
        doctor._check_mcp(r, "claude", tmp_path)
        assert r.checks[0].ok is False and r.checks[0].optional is True

    def test_mcp_registered_and_matching_passes(self, tmp_path: Path):
        attach_kit.register("claude", project_root=tmp_path)
        r = Report()
        doctor._check_mcp(r, "claude", tmp_path)
        assert r.checks[0].ok is True

    def test_mcp_wrong_interpreter_is_flagged(self, tmp_path: Path):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"a2a-hub": {
            "command": "C:/elsewhere/python.exe", "args": ["x"]}}}), encoding="utf-8")
        r = Report()
        doctor._check_mcp(r, "claude", tmp_path)
        assert r.checks[0].ok is False
        assert "解释器不一致" in r.checks[0].detail


# --------------------------------------------------------------------------- #
# 端到端探针
# --------------------------------------------------------------------------- #


class TestProbe:
    def test_probe_completes_a_real_round_trip(self, settings):
        """探针要的是**真往返**：事件流从 adapter 回来、回显内容对得上 token。"""
        from a2a_hub.registry import bootstrap

        reg = bootstrap(settings)
        asyncio.run(reg.check_health(force=True))
        res = asyncio.run(doctor._probe(reg))
        assert res["ok"] is True, res
        assert res["agent"] == "echo-a"
        assert res["state"] == "completed"
        assert res["echoed"] is True

    def test_probe_token_is_random_per_run(self, settings):
        from a2a_hub.registry import bootstrap

        reg = bootstrap(settings)
        asyncio.run(reg.check_health(force=True))
        a = asyncio.run(doctor._probe(reg))
        b = asyncio.run(doctor._probe(reg))
        assert a["token"] != b["token"], "固定 token 等于没在验真"

    def test_probe_reports_failure_instead_of_raising(self):
        """探针自己炸了要如实回报，而不是把整份体检带崩。"""

        class Rec:
            id = "boom"

        class Boom:
            def has(self, _a):
                return True

            def get(self, _a):
                return Rec()

            def new_task(self, *_a):
                raise RuntimeError("adapter 崩了")

            def list_records(self):
                return []

        res = asyncio.run(doctor._probe(Boom()))
        assert res["ok"] is False and "RuntimeError" in res["detail"]

    def test_probe_picks_any_healthy_agent_when_echo_absent(self, registry):
        """没有 `echo` 时退到第一个 healthy 的——探针不该依赖某个具体名字。"""
        asyncio.run(registry.check_health(force=True))
        res = asyncio.run(doctor._probe(registry))
        assert res["ok"] is True, res
        assert res["agent"] in {r.id for r in registry.list_records()}


# --------------------------------------------------------------------------- #
# 整体运行与渲染
# --------------------------------------------------------------------------- #


class TestRunAndRender:
    def test_run_without_probe(self, settings, tmp_path: Path):
        report = asyncio.run(doctor.run(
            settings, probe=False, host="claude", project_root=tmp_path))
        keys = {c.key for c in report.checks}
        assert {"env.python", "env.agents", "agents.ready", "social", "mcp"} <= keys
        assert report.probe == {}
        assert report.ok is True  # 临时环境里没有 members.yaml / mcp 登记，但都可选

    def test_run_with_probe(self, settings, tmp_path: Path):
        report = asyncio.run(doctor.run(
            settings, probe=True, host="claude", project_root=tmp_path))
        assert report.probe.get("ok") is True
        assert any(c.key == "probe" and c.ok for c in report.checks)

    def test_run_sees_registered_mcp(self, settings, tmp_path: Path):
        attach_kit.register("claude", project_root=tmp_path)
        report = asyncio.run(doctor.run(
            settings, probe=False, host="claude", project_root=tmp_path))
        mcp = next(c for c in report.checks if c.key == "mcp")
        assert mcp.ok is True

    def test_render_says_ready_when_ok(self):
        r = Report(checks=[Check("a", "运行环境", ok=True, detail="Python 3.13")])
        text = doctor.render(r, color=False)
        assert "就绪，可以用" in text and "✓ 运行环境" in text

    def test_render_marks_optional_failure_differently(self):
        """可选不合格标 `!` 而不是 `✗`——它不该跟真事故混在一起。"""
        r = Report(checks=[
            Check("a", "运行环境", ok=True),
            Check("social", "社交门禁", ok=False, optional=True, detail="未启用"),
        ])
        text = doctor.render(r, color=False)
        assert "! 社交门禁（可选）" in text
        assert "✗" not in text

    def test_render_lists_next_steps(self):
        r = Report(checks=[Check("env.agents", "agent 注册表", ok=False, detail="找不到")])
        doctor._add(r, r.checks[0])
        text = doctor.render(r, color=False)
        assert "下一步" in text and "1." in text

    def test_render_color_wraps_in_ansi(self):
        r = Report(checks=[Check("a", "运行环境", ok=True)])
        assert "\033[32m" in doctor.render(r, color=True)
