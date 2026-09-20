"""JSON-RPC 分发层测试。"""

from __future__ import annotations

import pytest

from a2a_hub.models import JsonRpcErrorCodes


def rpc(method: str, params: dict | None = None, req_id: int | str = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


@pytest.mark.asyncio
class TestEnvelope:
    async def test_invalid_jsonrpc_version_rejected(self, dispatcher):
        res = await dispatcher.handle({"id": 1, "method": "agents/list", "jsonrpc": "1.0"})
        assert res["error"]["code"] == JsonRpcErrorCodes.INVALID_REQUEST

    async def test_missing_method_rejected(self, dispatcher):
        res = await dispatcher.handle({"jsonrpc": "2.0", "id": 1})
        assert res["error"]["code"] == JsonRpcErrorCodes.INVALID_REQUEST

    async def test_unknown_method_reports_supported(self, dispatcher):
        res = await dispatcher.handle(rpc("nope/nope"))
        assert res["error"]["code"] == JsonRpcErrorCodes.METHOD_NOT_FOUND
        assert "message/send" in res["error"]["data"]["supported"]

    async def test_id_is_echoed_back(self, dispatcher):
        res = await dispatcher.handle(rpc("agents/list", req_id="abc-1"))
        assert res["id"] == "abc-1"

    async def test_missing_required_param(self, dispatcher):
        res = await dispatcher.handle(rpc("message/send", {"agentId": "echo-a"}))
        assert res["error"]["code"] == JsonRpcErrorCodes.INVALID_PARAMS
        assert "message" in res["error"]["message"]


@pytest.mark.asyncio
class TestMessageSend:
    async def test_send_to_explicit_agent(self, dispatcher):
        res = await dispatcher.handle(
            rpc("message/send", {"agentId": "echo-a", "message": "你好"})
        )
        task = res["result"]
        assert task["kind"] == "task"
        assert task["status"]["state"] == "completed"
        assert "[A] 你好" in task["artifacts"][0]["parts"][0]["text"]

    async def test_send_accepts_structured_message(self, dispatcher):
        res = await dispatcher.handle(rpc("message/send", {
            "agentId": "echo-a",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "结构化"}]},
        }))
        assert res["result"]["status"]["state"] == "completed"

    async def test_auto_routing_when_agent_omitted(self, dispatcher):
        res = await dispatcher.handle(rpc("message/send", {"message": "帮我评审代码"}))
        assert res["result"]["status"]["state"] == "completed"

    async def test_unknown_agent_lists_available(self, dispatcher):
        res = await dispatcher.handle(
            rpc("message/send", {"agentId": "ghost", "message": "x"})
        )
        assert res["error"]["code"] == JsonRpcErrorCodes.INVALID_PARAMS
        assert "echo-a" in res["error"]["data"]["available"]

    async def test_scoped_agent_endpoint_implies_agent_id(self, dispatcher):
        res = await dispatcher.handle(rpc("message/send", {"message": "hi"}), scoped_agent="echo-b")
        assert res["result"]["status"]["state"] == "completed"
        assert "[B]" in res["result"]["artifacts"][0]["parts"][0]["text"]

    async def test_history_is_preserved(self, dispatcher):
        res = await dispatcher.handle(rpc("message/send", {"agentId": "echo-a", "message": "历史"}))
        history = res["result"]["history"]
        assert len(history) >= 2
        assert history[0]["role"] == "user"
        assert history[-1]["role"] == "agent"


@pytest.mark.asyncio
class TestTasks:
    async def test_get_task_roundtrip(self, dispatcher):
        sent = await dispatcher.handle(rpc("message/send", {"agentId": "echo-a", "message": "查询"}))
        task_id = sent["result"]["id"]
        got = await dispatcher.handle(rpc("tasks/get", {"id": task_id}))
        assert got["result"]["id"] == task_id

    async def test_get_unknown_task(self, dispatcher):
        res = await dispatcher.handle(rpc("tasks/get", {"id": "task-nope"}))
        assert res["error"]["code"] == JsonRpcErrorCodes.TASK_NOT_FOUND

    async def test_history_length_truncates(self, dispatcher):
        sent = await dispatcher.handle(rpc("message/send", {"agentId": "echo-a", "message": "x"}))
        task_id = sent["result"]["id"]
        got = await dispatcher.handle(rpc("tasks/get", {"id": task_id, "historyLength": 1}))
        assert len(got["result"]["history"]) == 1

    async def test_cancel_terminal_task_errors(self, dispatcher):
        sent = await dispatcher.handle(rpc("message/send", {"agentId": "echo-a", "message": "x"}))
        res = await dispatcher.handle(rpc("tasks/cancel", {"id": sent["result"]["id"]}))
        assert res["error"]["code"] == JsonRpcErrorCodes.TASK_NOT_CANCELABLE

    async def test_push_notification_not_supported(self, dispatcher):
        res = await dispatcher.handle(rpc("tasks/pushNotificationConfig/set", {"id": "x"}))
        assert res["error"]["code"] == JsonRpcErrorCodes.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
class TestStreaming:
    async def test_message_stream_yields_events(self, dispatcher):
        gen = await dispatcher.handle(
            rpc("message/stream", {"agentId": "echo-a", "message": "流式"})
        )
        kinds = []
        async for event in gen:
            kinds.append(event["kind"])
        assert kinds[0] == "status-update"
        assert "artifact-update" in kinds
        assert kinds[-1] == "status-update"


@pytest.mark.asyncio
class TestHubExtensions:
    async def test_agents_list(self, dispatcher):
        res = await dispatcher.handle(rpc("agents/list"))
        assert res["result"]["count"] == 4
        ids = [a["id"] for a in res["result"]["agents"]]
        assert "echo-a" in ids

    async def test_agents_card_hub_level(self, dispatcher):
        res = await dispatcher.handle(rpc("agents/card"))
        assert res["result"]["protocolVersion"] == "0.3.0"

    async def test_agents_card_specific(self, dispatcher):
        res = await dispatcher.handle(rpc("agents/card", {"agentId": "echo-a"}))
        assert res["result"]["name"] == "回显 A"

    async def test_collab_modes_lists_four(self, dispatcher):
        res = await dispatcher.handle(rpc("collab/modes"))
        ids = [m["id"] for m in res["result"]["modes"]]
        assert ids == ["delegate", "broadcast", "pipeline", "roundtable"]

    async def test_collab_run_broadcast(self, dispatcher):
        res = await dispatcher.handle(rpc("collab/run", {
            "mode": "broadcast",
            "prompt": "写一份报告",
            "options": {"topK": 2},
        }))
        run = res["result"]
        assert run["status"] == "completed"
        # topK=2 且未指定 synthesizer → 2 个应答步 + 结构化汇总（不额外加步）
        assert len(run["steps"]) == 2
        assert run["result"]

    async def test_collab_run_with_synthesizer_adds_step(self, dispatcher):
        res = await dispatcher.handle(rpc("collab/run", {
            "mode": "broadcast",
            "prompt": "写一份报告",
            "options": {"topK": 2, "synthesizer": "echo-a"},
        }))
        run = res["result"]
        assert len(run["steps"]) == 3
        assert run["steps"][-1]["label"] == "综合"

    async def test_collab_unknown_mode(self, dispatcher):
        res = await dispatcher.handle(rpc("collab/run", {"mode": "nope", "prompt": "x"}))
        assert res["error"]["code"] == JsonRpcErrorCodes.INVALID_PARAMS

    async def test_collab_get_unknown_run(self, dispatcher):
        res = await dispatcher.handle(rpc("collab/get", {"runId": "collab-nope"}))
        assert res["error"]["code"] == JsonRpcErrorCodes.TASK_NOT_FOUND
