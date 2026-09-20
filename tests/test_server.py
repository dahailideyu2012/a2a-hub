"""HTTP 服务测试 —— 覆盖发现端点、JSON-RPC、SSE 与协同 API。"""

from __future__ import annotations

import json

import pytest


class TestDiscovery:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["agents"] == 4

    def test_hub_agent_card_at_well_known(self, client):
        r = client.get("/.well-known/agent.json")
        assert r.status_code == 200
        card = r.json()
        assert card["protocolVersion"] == "0.3.0"
        assert card["capabilities"]["streaming"] is True
        assert any(s["id"] == "multi-agent-collaboration" for s in card["skills"])

    def test_agent_card_alias_path(self, client):
        assert client.get("/.well-known/agent-card.json").status_code == 200

    def test_list_agents(self, client):
        r = client.get("/agents")
        assert r.status_code == 200
        assert r.json()["count"] == 4

    def test_per_agent_card(self, client):
        r = client.get("/agents/echo-a/.well-known/agent.json")
        assert r.status_code == 200
        assert r.json()["name"] == "回显 A"

    def test_unknown_agent_card_404(self, client):
        assert client.get("/agents/ghost/.well-known/agent.json").status_code == 404

    def test_agent_detail(self, client):
        r = client.get("/agents/echo-a")
        assert r.status_code == 200
        assert r.json()["type"] == "echo"

    def test_root_gives_discovery_hint(self, client):
        r = client.get("/")
        body = r.json()
        assert "agentCard" in body
        assert "message/send" in body["methods"]


class TestJsonRpc:
    def test_message_send_blocking(self, client):
        r = client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"agentId": "echo-a", "message": "HTTP 测试"},
        })
        assert r.status_code == 200
        task = r.json()["result"]
        assert task["status"]["state"] == "completed"
        assert "[A] HTTP 测试" in task["artifacts"][0]["parts"][0]["text"]

    def test_agent_scoped_endpoint(self, client):
        r = client.post("/agents/echo-b/", json={
            "jsonrpc": "2.0", "id": 2, "method": "message/send",
            "params": {"message": "scoped"},
        })
        assert r.status_code == 200
        assert "[B]" in r.json()["result"]["artifacts"][0]["parts"][0]["text"]

    def test_non_blocking_send_returns_immediately(self, client):
        r = client.post("/", json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {"agentId": "echo-a", "message": "异步",
                       "configuration": {"blocking": False}},
        })
        assert r.status_code == 200
        assert r.json()["result"]["id"].startswith("task-")

    def test_method_not_found(self, client):
        r = client.post("/", json={"jsonrpc": "2.0", "id": 4, "method": "bogus"})
        assert r.json()["error"]["code"] == -32601

    def test_invalid_params(self, client):
        r = client.post("/", json={
            "jsonrpc": "2.0", "id": 5, "method": "message/send", "params": {},
        })
        assert r.json()["error"]["code"] == -32602

    def test_agents_list_extension(self, client):
        r = client.post("/", json={"jsonrpc": "2.0", "id": 6, "method": "agents/list"})
        assert r.json()["result"]["count"] == 4


class TestStreaming:
    def test_message_stream_sse(self, client):
        with client.stream("POST", "/", json={
            "jsonrpc": "2.0", "id": 7, "method": "message/stream",
            "params": {"agentId": "echo-a", "message": "流式"},
        }) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            body = "".join(r.iter_text())

        assert "event: status-update" in body
        assert "event: artifact-update" in body
        assert "event: done" in body
        assert "流式" in body

    def test_message_stream_is_valid_sse_framing(self, client):
        with client.stream("POST", "/", json={
            "jsonrpc": "2.0", "id": 8, "method": "message/stream",
            "params": {"agentId": "echo-a", "message": "framing"},
        }) as r:
            raw = "".join(r.iter_text())

        events = [b for b in raw.split("\n\n") if b.strip()]
        assert events
        for block in events:
            if block.startswith(": "):
                continue  # keepalive
            assert block.startswith("event: ") or block.startswith("data: ")

    def test_task_events_endpoint_after_completion(self, client):
        send = client.post("/", json={
            "jsonrpc": "2.0", "id": 9, "method": "message/send",
            "params": {"agentId": "echo-a", "message": "已完成"},
        })
        task_id = send.json()["result"]["id"]
        with client.stream("GET", f"/tasks/{task_id}/events") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "event: task" in body
        assert "event: done" in body

    def test_unknown_task_events_404(self, client):
        assert client.get("/tasks/task-nope/events").status_code == 404


class TestTasksApi:
    def test_list_and_get(self, client):
        client.post("/", json={
            "jsonrpc": "2.0", "id": 10, "method": "message/send",
            "params": {"agentId": "echo-a", "message": "列表"},
        })
        lst = client.get("/tasks").json()
        assert lst["count"] >= 1
        tid = lst["tasks"][0]["id"]
        detail = client.get(f"/tasks/{tid}").json()
        assert detail["id"] == tid
        assert detail["agentId"] == "echo-a"

    def test_get_unknown_task_404(self, client):
        assert client.get("/tasks/task-nope").status_code == 404

    def test_cancel_terminal_task_400(self, client):
        send = client.post("/", json={
            "jsonrpc": "2.0", "id": 11, "method": "message/send",
            "params": {"agentId": "echo-a", "message": "x"},
        })
        tid = send.json()["result"]["id"]
        assert client.post(f"/tasks/{tid}/cancel").status_code == 400


class TestCollabApi:
    def test_broadcast_blocking(self, client):
        r = client.post("/collab", json={
            "mode": "broadcast", "prompt": "写一份报告",
            "options": {"topK": 2}, "blocking": True,
        })
        assert r.status_code == 200
        run = r.json()
        assert run["status"] == "completed"
        assert len(run["steps"]) == 2

    def test_missing_prompt_422(self, client):
        assert client.post("/collab", json={"mode": "delegate"}).status_code == 422

    def test_invalid_mode_422(self, client):
        r = client.post("/collab", json={"mode": "telepathy", "prompt": "x"})
        assert r.status_code == 422

    def test_list_and_get_run(self, client):
        run = client.post("/collab", json={
            "mode": "delegate", "prompt": "任务", "agentIds": ["echo-a"], "blocking": True,
        }).json()
        assert client.get("/collab").json()["count"] >= 1
        got = client.get(f"/collab/{run['id']}").json()
        assert got["id"] == run["id"]

    def test_unknown_run_404(self, client):
        assert client.get("/collab/collab-nope").status_code == 404

    def test_collab_events_sse(self, client):
        run = client.post("/collab", json={
            "mode": "delegate", "prompt": "事件", "agentIds": ["echo-a"], "blocking": True,
        }).json()
        with client.stream("GET", f"/collab/{run['id']}/events") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "event: collab" in body
        assert "collab-snapshot" in body


class TestConsole:
    def test_console_page_served(self, client):
        r = client.get("/console")
        assert r.status_code == 200
        assert "A2A Hub" in r.text

    def test_static_assets(self, client):
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
