"""CLI 参数解析与进程内执行的测试。

这块值得单独测：argparse 的重复参数、`args.json` 缺失之类的错误
只会在真正敲命令时才暴露，而单元测试碰不到。
"""

from __future__ import annotations

import pytest

from a2a_hub.cli import build_parser, main, resolve_public_url


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


class TestParser:
    def test_all_subcommands_parse(self):
        assert parse(["serve"]).cmd == "serve"
        assert parse(["agents"]).cmd == "agents"
        assert parse(["health"]).cmd == "health"
        assert parse(["card"]).cmd == "card"
        assert parse(["modes"]).cmd == "modes"
        assert parse(["ask", "问题"]).cmd == "ask"
        assert parse(["collab", "broadcast", "任务"]).cmd == "collab"

    def test_agents_parser_has_no_duplicate_json_flag(self):
        # 回归：曾把 --json 加了两次，argparse 直接抛 ArgumentError
        args = parse(["agents", "--json"])
        assert args.json is True
        assert parse(["agents"]).json is False

    def test_every_subcommand_exposes_json_attribute(self):
        for argv in (["health"], ["card"], ["modes"], ["ask", "x"],
                     ["collab", "delegate", "x"]):
            assert hasattr(parse(argv), "json"), argv

    def test_serve_options(self):
        args = parse(["serve", "--port", "9000", "--host", "127.0.0.1",
                      "--public-url", "https://hub.example.com", "--reload"])
        assert args.port == 9000
        assert args.host == "127.0.0.1"
        assert args.public_url == "https://hub.example.com"
        assert args.reload is True

    def test_ask_options(self):
        args = parse(["ask", "评审代码", "--agent", "echo-a"])
        assert args.prompt == "评审代码"
        assert args.agent == "echo-a"

    def test_remote_options_present_everywhere(self):
        for argv in (["agents"], ["health"], ["card"], ["modes"],
                     ["ask", "x"], ["collab", "delegate", "x"]):
            a = parse(argv + ["--url", "http://h:1", "--token", "t"])
            assert a.url == "http://h:1"
            assert a.token == "t"

    def test_collab_options(self):
        args = parse([
            "collab", "roundtable", "议题",
            "--agents", "echo-a,echo-b",
            "--rounds", "3",
            "--top-k", "2",
            "--synthesizer", "echo-a",
            "--reviewer", "echo-b",
        ])
        assert args.mode == "roundtable"
        assert args.agents == "echo-a,echo-b"
        assert args.rounds == 3
        assert args.top_k == 2
        assert args.synthesizer == "echo-a"
        assert args.reviewer == "echo-b"

    def test_invalid_mode_rejected(self):
        with pytest.raises(SystemExit):
            parse(["collab", "telepathy", "x"])


class TestInProcessRun:
    """通过 main() 跑真实的进程内路径（不需要起服务）。"""

    def test_agents_command(self, settings, capsys):
        rc = main(["agents"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "echo-a" in out
        assert "代码评审" in out

    def test_agents_json_command(self, settings, capsys):
        rc = main(["agents", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        # 输出的是 agent 快照数组（与 /agents 接口同构）
        assert '"id": "echo-a"' in out
        assert '"skills"' in out

    def test_health_command(self, settings, capsys):
        rc = main(["health"])
        assert rc == 0
        assert "echo-a" in capsys.readouterr().out

    def test_card_command_hub_level(self, settings, capsys):
        rc = main(["card"])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"protocolVersion": "0.3.0"' in out
        assert "multi-agent-collaboration" in out

    def test_card_command_for_agent(self, settings, capsys):
        rc = main(["card", "--agent", "echo-a"])
        assert rc == 0
        assert "回显 A" in capsys.readouterr().out

    def test_modes_command(self, settings, capsys):
        rc = main(["modes"])
        assert rc == 0
        out = capsys.readouterr().out
        for mode in ("delegate", "broadcast", "pipeline", "roundtable"):
            assert mode in out

    def test_ask_explicit_agent(self, settings, capsys):
        rc = main(["ask", "你好", "--agent", "echo-a"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[A] 你好" in out
        assert "completed" in out

    def test_ask_auto_routes(self, settings, capsys):
        rc = main(["ask", "帮我评审这段代码"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "自动路由" in out

    def test_ask_unknown_agent_fails(self, settings, capsys):
        rc = main(["ask", "x", "--agent", "ghost"])
        assert rc == 2

    def test_ask_failed_task_returns_nonzero(self, settings, capsys):
        rc = main(["ask", "BOOM", "--agent", "echo-fail"])
        assert rc == 1
        assert "failed" in capsys.readouterr().out

    def test_collab_delegate(self, settings, capsys):
        rc = main(["collab", "delegate", "写点什么", "--agents", "echo-b"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "协同结果" in out
        assert "[B]" in out

    def test_collab_broadcast_with_structured_merge(self, settings, capsys):
        rc = main(["collab", "broadcast", "议题", "--top-k", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "结构化汇总" in out

    def test_collab_roundtable(self, settings, capsys):
        rc = main(["collab", "roundtable", "议题", "--top-k", "2", "--rounds", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "第 1 轮结束" in out
        assert "第 2 轮结束" in out

    def test_collab_pipeline_default(self, settings, capsys):
        rc = main(["collab", "pipeline", "做一个功能", "--top-k", "2"])
        assert rc == 0
        assert "协同结果" in capsys.readouterr().out


class TestPublicUrlResolution:
    """Agent Card 里的对外地址必须和实际监听地址一致。

    回归：`serve --port 9000` 曾在卡片里保留默认的 8080，
    对端按照卡片地址回连会直接失败。
    """

    def test_explicit_flag_wins(self):
        assert resolve_public_url(
            "http://localhost:8080", "0.0.0.0", 9000, "https://hub.example.com/"
        ) == "https://hub.example.com"

    def test_follows_actual_port_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
        assert resolve_public_url("http://localhost:8080", "0.0.0.0", 9000) == \
            "http://localhost:9000"

    def test_wildcard_host_becomes_localhost(self, monkeypatch):
        monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
        assert resolve_public_url("http://localhost:8080", "::", 9000) == \
            "http://localhost:9000"

    def test_concrete_host_is_preserved(self, monkeypatch):
        monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
        assert resolve_public_url("http://localhost:8080", "127.0.0.1", 8099) == \
            "http://127.0.0.1:8099"

    def test_env_var_beats_port_derivation(self, monkeypatch):
        monkeypatch.setenv("A2A_PUBLIC_URL", "https://hub.example.com")
        assert resolve_public_url("http://localhost:8080", "0.0.0.0", 9000) == \
            "http://localhost:8080"
