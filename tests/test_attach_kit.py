"""接入工具箱的回归 —— 守「装」这一步。

`attach` 的其它形态都只是**给人看的文本**，只有 `--register` 会让机器状态
发生变化（写 host 的 mcp.json）。凡是要写用户文件的地方，就得守三件事：

1. **幂等**：重复执行不堆叠、不产生噪声 diff。
2. **不越界**：只动自己那条，别人配的 server、host 自己加的 `disabled`/`env`
   一律原样保留——那是用户的配置，不是我们的。
3. **写前备份**：改之前先落一份 `.bak-<时间戳>`。

还有一个不显眼但最常翻车的点：`command` 必须是**装了项目依赖的解释器**，
不能是裸 `python`。MCP server 是进程内 import `a2a_hub` 的，裸解释器会
静默失败——配置里那一行看着完全正常。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_hub import attach_kit  # noqa: E402


class TestHostRegistry:
    def test_keys_are_unique(self):
        keys = [h.key for h in attach_kit.HOSTS]
        assert len(keys) == len(set(keys))

    def test_every_host_declares_a_path(self):
        for h in attach_kit.HOSTS:
            assert h.path, f"{h.key} 没有配置文件路径"

    def test_workbuddy_is_the_default_global_host(self):
        """本机把 Hub 用起来的主通道就是 WorkBuddy，默认必须指向它。"""
        assert attach_kit.DEFAULT_HOST == "workbuddy"
        host = attach_kit.by_key("workbuddy")
        assert host is not None
        p = attach_kit.resolve_path(host)
        assert p.name == "mcp.json"
        assert ".workbuddy" in str(p)

    def test_project_hosts_resolve_under_project_root(self, tmp_path: Path):
        for key, rel in (("claude", ".mcp.json"), ("cursor", ".cursor/mcp.json")):
            host = attach_kit.by_key(key)
            got = attach_kit.resolve_path(host, project_root=tmp_path)
            assert got == (tmp_path / rel).resolve()

    def test_codex_is_not_auto_writable(self):
        """TOML 没有标准库写入器，硬拼会破坏用户文件——只能给片段。"""
        host = attach_kit.by_key("codex")
        assert host is not None and host.writable is False

    def test_unknown_key_is_none(self):
        assert attach_kit.by_key("nope") is None


class TestRegister:
    def test_creates_file_when_missing(self, tmp_path: Path):
        res = attach_kit.register("claude", project_root=tmp_path)
        assert res["ok"] and res["changed"] and res["created"]
        assert res["backup"] is None, "新建文件不该产生备份"

        cfg = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        entry = cfg["mcpServers"]["a2a-hub"]
        assert entry["args"] and entry["args"][0].endswith("mcp_server.py")

    def test_command_is_a_real_interpreter_not_bare_python(self, tmp_path: Path):
        res = attach_kit.register("claude", project_root=tmp_path)
        cfg = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
        cmd = cfg["mcpServers"]["a2a-hub"]["command"]
        assert cmd != "python"
        assert cmd.endswith(("python.exe", "python", "python3"))

    def test_idempotent_second_run_changes_nothing(self, tmp_path: Path):
        attach_kit.register("claude", project_root=tmp_path)
        again = attach_kit.register("claude", project_root=tmp_path)
        assert again["ok"] and again["changed"] is False
        assert again["backup"] is None
        assert not list(tmp_path.glob("*.bak-*")), "无变化却产生了备份"

    def test_backs_up_before_overwrite(self, tmp_path: Path):
        p = tmp_path / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {"a2a-hub": {
            "command": "C:/old/python.exe", "args": ["C:/old/mcp_server.py"]}}}),
            encoding="utf-8")
        res = attach_kit.register("claude", project_root=tmp_path)
        assert res["changed"] and res["backup"]
        assert Path(res["backup"]).exists()
        old = json.loads(Path(res["backup"]).read_text(encoding="utf-8"))
        assert old["mcpServers"]["a2a-hub"]["command"] == "C:/old/python.exe"

    def test_preserves_host_added_fields(self, tmp_path: Path):
        """`disabled` / `env` 是用户或 host 加的，更新时不能顺手删掉。"""
        p = tmp_path / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {"a2a-hub": {
            "command": "C:/old/python.exe", "args": ["x"],
            "disabled": False, "env": {"FOO": "bar"}}}}), encoding="utf-8")
        attach_kit.register("claude", project_root=tmp_path)
        entry = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["a2a-hub"]
        assert entry["disabled"] is False
        assert entry["env"] == {"FOO": "bar"}
        assert entry["command"] != "C:/old/python.exe"

    def test_never_touches_other_servers(self, tmp_path: Path):
        p = tmp_path / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {"other": {
            "command": "keep-me", "args": []}}}), encoding="utf-8")
        attach_kit.register("claude", project_root=tmp_path)
        cfg = json.loads(p.read_text(encoding="utf-8"))
        assert cfg["mcpServers"]["other"]["command"] == "keep-me"
        assert "a2a-hub" in cfg["mcpServers"]

    def test_refuses_non_writable_host(self, tmp_path: Path):
        res = attach_kit.register("codex", project_root=tmp_path)
        assert res["ok"] is False and "TOML" in res["error"]

    def test_unknown_host_reports_error(self, tmp_path: Path):
        res = attach_kit.register("nope", project_root=tmp_path)
        assert res["ok"] is False and "未知" in res["error"]

    def test_survives_corrupt_json(self, tmp_path: Path):
        """配置文件被写坏了也不能把用户的文件陪葬——退回空 dict 重建。"""
        p = tmp_path / ".mcp.json"
        p.write_text("{ 这不是合法 JSON", encoding="utf-8")
        res = attach_kit.register("claude", project_root=tmp_path)
        assert res["ok"] and res["backup"], "覆盖坏文件前必须先备份"


class TestInspect:
    def test_not_registered(self, tmp_path: Path):
        info = attach_kit.inspect("claude", project_root=tmp_path)
        assert info["known"] and info["registered"] is False

    def test_registered_and_matching(self, tmp_path: Path):
        attach_kit.register("claude", project_root=tmp_path)
        info = attach_kit.inspect("claude", project_root=tmp_path)
        assert info["registered"] and info["command_matches"]

    def test_detects_wrong_interpreter(self, tmp_path: Path):
        """解释器不一致是**最阴**的故障：配置看着完全正常，链路上不去。"""
        p = tmp_path / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {"a2a-hub": {
            "command": "C:/somewhere/else/python.exe", "args": ["x"]}}}),
            encoding="utf-8")
        info = attach_kit.inspect("claude", project_root=tmp_path)
        assert info["registered"] and info["command_matches"] is False
        assert info["expected_command"] != "C:/somewhere/else/python.exe"

    def test_unknown_host(self):
        assert attach_kit.inspect("nope")["known"] is False


@pytest.mark.parametrize("key", [h.key for h in attach_kit.HOSTS])
def test_inspect_never_raises(key: str, tmp_path: Path):
    """体检要能对任何 host 出结论，不能因为路径不存在就炸。"""
    info = attach_kit.inspect(key, project_root=tmp_path)
    assert "registered" in info
