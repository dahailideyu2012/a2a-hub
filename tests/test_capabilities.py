"""能力清单与「接入包」的回归。

守的是「各 agent 怎么套用 Hub 的能力」这条链路：

1. **同源**：CLI `capabilities` / HTTP `GET /capabilities` / RPC
   `hub/capabilities` 必须来自同一份 ``CAPABILITIES``——三处各抄一份必然漂移，
   表现为「文档说能调、实际调不通」。
2. **清单自洽**：id 不重复、每条至少有一种接法、写操作要标 ``write``。
3. **给 prompt 型 agent 的片段要短**：这段要常驻上下文窗口，啰嗦是长期成本。
4. **`attach --out` 幂等**：它可能写进 ``CLAUDE.md`` / ``AGENTS.md`` 这类
   **用户自己的文件**，必须能反复执行而不堆叠、不覆盖原有内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_hub.capabilities import (  # noqa: E402
    CAPABILITIES,
    MARK_BEGIN,
    MARK_END,
    by_id,
    mcp_server_config,
    render_attach,
    render_prompt,
    render_table,
)


class TestManifest:
    def test_ids_unique(self):
        ids = [c.id for c in CAPABILITIES]
        assert len(ids) == len(set(ids))

    def test_every_capability_has_a_way_to_call_it(self):
        """一条能力如果四种接法全空，等于没登记——套用方无从下手。"""
        for c in CAPABILITIES:
            assert any([c.cli, c.rpc, c.http, c.mcp]), f"{c.id} 没有任何接法"

    def test_every_capability_explains_itself(self):
        for c in CAPABILITIES:
            assert c.summary.strip(), f"{c.id} 缺 summary"

    def test_write_ops_are_marked(self):
        """写操作必须标出来，套用方才知道要先征得同意。"""
        for cap_id in ("social-write", "social-init"):
            c = by_id(cap_id)
            assert c is not None and c.write is True

    def test_delegate_gated_capabilities_declare_scope(self):
        for cap_id in ("ask", "collab"):
            c = by_id(cap_id)
            assert c is not None and c.scope == "delegate"

    def test_by_id_returns_none_for_unknown(self):
        assert by_id("nope") is None


class TestRendering:
    def test_table_mentions_every_transport_it_has(self):
        text = render_table()
        for c in CAPABILITIES:
            assert c.id in text
        for tool in ("a2a_delegate", "a2a_social", "a2a_social_init"):
            assert tool in text

    def test_prompt_stays_short(self):
        """prompt 片段要常驻上下文——超过十几行就是长期成本。"""
        text = render_prompt(base_url="http://h:8080", identity="agent:codex")
        assert text.startswith("[A2A Hub 协作能力]")
        assert len(text.splitlines()) <= 16

    def test_prompt_carries_identity_and_discipline(self):
        text = render_prompt(identity="agent:codex")
        assert "agent:codex" in text
        assert "token" in text  # 纪律：别把内部信息写进产出

    def test_attach_mcp_uses_current_interpreter(self):
        """MCP server 是进程内 import a2a_hub 的，解释器路径不能想当然写 python。"""
        cfg = mcp_server_config()
        cmd = cfg["mcpServers"]["a2a-hub"]["command"]
        assert cmd and cmd != "python"
        assert cmd.endswith(("python.exe", "python", "python3"))

    def test_attach_each_transport_renders(self):
        for t in ("mcp", "cli", "http", "prompt"):
            text = render_attach(t, identity="agent:codex", base_url="http://h:8080")
            assert text.strip(), f"{t} 渲染为空"

    def test_attach_http_points_at_root_for_rpc(self):
        """JSON-RPC 端点是根路径；写成 /rpc 会 404。"""
        text = render_attach("http", base_url="http://h:8080")
        assert "POST http://h:8080/" in text
        assert "/rpc" not in text


class TestHttpAndRpc:
    def test_get_capabilities(self, client):
        r = client.get("/capabilities", params={"base_url": "http://h:8080"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["capabilities"]) == len(CAPABILITIES)
        assert body["meta"]["transports"]["rpc"]  # 带调用方式说明

    def test_capabilities_needs_no_auth(self, client):
        """新 agent 得先看到说明书才知道怎么拿 token，所以它本身不能要 token。"""
        assert client.get("/capabilities").status_code == 200

    def test_rpc_hub_capabilities(self, client):
        r = client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "hub/capabilities", "params": {}})
        assert r.status_code == 200
        res = r.json()["result"]
        assert len(res["capabilities"]) == len(CAPABILITIES)

    def test_method_is_advertised(self, client):
        """清单里的方法要在 supported 列表里——否则「文档说能调、实际 404」。"""
        card = client.get("/.well-known/agent.json").json()
        methods = card.get("methods") or []
        # Hub 卡片不一定带 methods，那就查 RPC 报错里回显的 supported
        r = client.post("/", json={
            "jsonrpc": "2.0", "id": 2, "method": "no/such", "params": {}})
        supported = r.json()["error"]["data"]["supported"]
        assert "hub/capabilities" in supported
        if methods:
            assert "hub/capabilities" in methods


class TestAttachCli:
    def test_guess_transport_by_declared_type(self):
        from a2a_hub.cli import _guess_transport

        # 这三个生态支持 MCP
        assert _guess_transport("codex") == "mcp"
        assert _guess_transport("claude-code") == "mcp"
        # 云端 agent 只能发 HTTP
        assert _guess_transport("qwen-office") == "http"
        # 猜不到就退回 cli（能跑命令的 agent 最多，这个默认最不容易给错）
        assert _guess_transport("no-such-agent") == "cli"
        assert _guess_transport(None) == "cli"

    def test_marked_block_is_idempotent_and_preserves_user_content(self, tmp_path: Path):
        from a2a_hub.cli import _write_marked_block

        f = tmp_path / "CLAUDE.md"
        f.write_text("# 我自己的说明\n\n原有内容不能被动。\n", encoding="utf-8")

        _write_marked_block(str(f), "第一版")
        _write_marked_block(str(f), "第二版")

        text = f.read_text(encoding="utf-8")
        assert text.count(MARK_BEGIN) == 1, "重复执行堆叠了多个标记块"
        assert text.count(MARK_END) == 1
        assert "第二版" in text and "第一版" not in text
        assert "原有内容不能被动" in text, "覆盖了用户原有内容"


class TestMcpInstructions:
    def test_initialize_carries_instructions(self):
        """MCP host 一接上就该知道这是什么、怎么用，不必先读文档。"""
        import json as _json
        import subprocess

        py = sys.executable
        server = str(ROOT / "mcp_server.py")
        payload = _json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}},
        }) + "\n"
        p = subprocess.run([py, server], input=payload.encode(), capture_output=True,
                           cwd=str(ROOT), timeout=180)
        out = [_json.loads(l) for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        instr = out[0]["result"].get("instructions", "")
        assert "a2a_agents" in instr and "a2a_delegate" in instr


@pytest.mark.parametrize("transport", ["mcp", "cli", "http", "prompt"])
def test_render_attach_never_empty(transport: str):
    assert render_attach(transport).strip()
