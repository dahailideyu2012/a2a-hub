from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path
import pytest
import workbuddy_mcp_server as server
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
 with server.LEASES_LOCK: server.LEASES.clear()
 server.HANDSHAKE_READY=False
 monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID","workbuddy-personal")
 monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL","https://hub.example.test")
 monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN","x"*48)
 yield
 with server.LEASES_LOCK: server.LEASES.clear()
def test_config_requires_https_and_long_token(monkeypatch):
 monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL","http://example.test")
 with pytest.raises(RuntimeError,match="HTTPS"): server._config()
 monkeypatch.setenv("A2A_WORKBUDDY_HUB_URL","http://127.0.0.1:8080")
 assert server._config()[0]=="http://127.0.0.1:8080"
 monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_TOKEN","short")
 with pytest.raises(RuntimeError,match="32"): server._config()
def test_claim_keeps_lease_secret_out_of_model_result(monkeypatch):
 task={"id":"task-1","agentId":"codex","status":{"state":"submitted"},"history":[{"parts":[{"kind":"text","text":"请审查代码"}]}]}
 monkeypatch.setattr(server,"_post",lambda path,payload: ({"accepted":True,"connector_id":"workbuddy-personal"} if path=="handshake" else {"claimed":True,"task":task,"lease":{"lease_token":"SECRET-LEASE-MATERIAL","lease_version":2,"lease_expires_at":time.time()+120}}))
 result=server.tool_claim({}); text=result["content"][0]["text"]
 assert "请审查代码" in text and "task-1" in text
 assert "SECRET-LEASE-MATERIAL" not in text
 assert server.LEASES["task-1"]["lease_version"]==2
def test_lifecycle_uses_private_lease_and_stable_result_key(monkeypatch):
 calls=[]
 monkeypatch.setattr(server,"_post",lambda path,payload:calls.append((path,payload)) or ({"accepted":True,"connector_id":"workbuddy-personal"} if path=="handshake" else ({"lease_expires_at":time.time()+120} if path.endswith("/renew") else {"ok":True})))
 with server.LEASES_LOCK: server.LEASES["task-2"]={"lease_token":"private-lease","lease_version":3,"lease_expires_at":time.time()+120}
 assert not server.tool_start({"task_id":"task-2"})["isError"]
 assert not server.tool_renew({"task_id":"task-2"})["isError"]
 assert not server.tool_progress({"task_id":"task-2","sequence":1,"message":"开始检查"})["isError"]
 assert not server.tool_result({"task_id":"task-2","state":"completed","output":"PASS"})["isError"]
 assert [c[0] for c in calls]==["tasks/task-2/start","tasks/task-2/renew","tasks/task-2/progress","tasks/task-2/result"]
 assert calls[-1][1]["result_key"]=="task-2:workbuddy-result-v1"
 assert calls[-1][1]["lease_token"]=="private-lease"
 assert "task-2" not in server.LEASES
def test_task_lifecycle_rejects_unknown_lease():
 # Exercise the same wrapper used by the MCP tools/call protocol handler.
 # Tool functions intentionally raise; call_tool converts exceptions into MCP error envelopes.
 result=server.call_tool("workbuddy_start_task",{"task_id":"not-claimed"})
 assert result["isError"] is True and "没有该任务" in result["content"][0]["text"]
def test_no_task_is_a_normal_result(monkeypatch):
 monkeypatch.setattr(server,"_post",lambda path,payload: ({"accepted":True,"connector_id":"workbuddy-personal"} if path=="handshake" else {"claimed":False}))
 result=server.tool_claim({})
 assert result["isError"] is False and "没有可领取" in result["content"][0]["text"]
def test_stdio_handshake_and_tools_list():
 payload="\n".join([json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}),json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"}),json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list"}),""])
 proc=subprocess.run([sys.executable,str(ROOT/"workbuddy_mcp_server.py")],input=payload,capture_output=True,text=True,encoding="utf-8",timeout=10,cwd=str(ROOT),env=os.environ.copy())
 assert proc.returncode==0
 replies=[json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
 assert [item["id"] for item in replies]==[1,2]
 tools=replies[1]["result"]["tools"]
 assert {item["name"] for item in tools}==set(server.TOOL_FUNCS)
 assert "SECRET" not in proc.stdout


def test_claim_refuses_second_active_task_until_result_is_submitted(monkeypatch):
 calls=[]
 task={"id":"task-active","agentId":"codex","status":{"state":"submitted"},"history":[{"parts":[{"kind":"text","text":"do work"}]}]}
 monkeypatch.setattr(server,"_post",lambda path,payload:calls.append(path) or ({"accepted":True,"connector_id":"workbuddy-personal"} if path=="handshake" else {
  "claimed":True,"task":task,"lease":{"lease_token":"private-lease-material","lease_version":1,"lease_expires_at":time.time()+120}
 }))
 first=server.tool_claim({})
 second=server.tool_claim({})
 assert first["isError"] is False
 assert second["isError"] is True
 assert calls==["handshake","claim"]
 assert "已有未完成任务" in second["content"][0]["text"]


def test_expired_local_lease_is_pruned_before_next_claim(monkeypatch):
 task={"id":"task-next","agentId":"codex","status":{"state":"submitted"},"history":[{"parts":[{"kind":"text","text":"next"}]}]}
 with server.LEASES_LOCK:
  server.LEASES["task-expired"]={"lease_token":"old-secret","lease_version":1,"lease_expires_at":time.time()-1}
 monkeypatch.setattr(server,"_post",lambda path,payload: ({"accepted":True,"connector_id":"workbuddy-personal"} if path=="handshake" else {
  "claimed":True,"task":task,"lease":{"lease_token":"new-secret-material","lease_version":1,"lease_expires_at":time.time()+120}
 }))
 result=server.tool_claim({})
 assert result["isError"] is False
 assert "task-expired" not in server.LEASES
 assert "task-next" in server.LEASES


def test_renew_updates_local_lease_expiry(monkeypatch):
 with server.LEASES_LOCK:
  server.LEASES["task-renew"]={"lease_token":"private-lease","lease_version":2,"lease_expires_at":time.time()+30}
 expected=time.time()+300
 monkeypatch.setattr(server,"_post",lambda path,payload:{"lease_expires_at":expected})
 result=server.tool_renew({"task_id":"task-renew"})
 assert result["isError"] is False
 assert server.LEASES["task-renew"]["lease_expires_at"]==expected


def test_progress_and_result_retry_transient_failures_with_stable_idempotency(monkeypatch):
 calls=[]
 with server.LEASES_LOCK:
  server.LEASES["task-retry"]={"lease_token":"private-lease","lease_version":4,"lease_expires_at":time.time()+120}
 def flaky_post(path,payload):
  calls.append((path,payload.copy()))
  if path.endswith("/progress") and sum(p.endswith("/progress") for p,_ in calls)==1:
   raise RuntimeError("Hub HTTP 503: temporary")
  if path.endswith("/result") and sum(p.endswith("/result") for p,_ in calls)==1:
   raise RuntimeError("无法连接 Hub：TimeoutError")
  return {"ok":True}
 monkeypatch.setattr(server,"_post",flaky_post)
 monkeypatch.setattr(server.time,"sleep",lambda _seconds:None)
 progress=server.tool_progress({"task_id":"task-retry","sequence":7,"message":"same update"})
 result=server.tool_result({"task_id":"task-retry","state":"completed","output":"done"})
 assert progress["isError"] is False and result["isError"] is False
 progress_calls=[payload for path,payload in calls if path.endswith("/progress")]
 result_calls=[payload for path,payload in calls if path.endswith("/result")]
 assert len(progress_calls)==2 and progress_calls[0]==progress_calls[1]
 assert len(result_calls)==2 and result_calls[0]==result_calls[1]
 assert result_calls[0]["result_key"]=="task-retry:workbuddy-result-v1"
 assert "task-retry" not in server.LEASES


def test_idempotent_retry_does_not_retry_conflicts_or_claim(monkeypatch):
 attempts=[]
 def conflict(_path,_payload):
  attempts.append("called")
  raise RuntimeError("Hub HTTP 409: lease conflict")
 monkeypatch.setattr(server,"_post",conflict)
 with pytest.raises(RuntimeError,match="409"):
  server._post_idempotent("tasks/task-1/progress",{"sequence":1,"message":"x"})
 assert len(attempts)==1

 claim_attempts=[]
 def fail_claim_after_handshake(path,payload):
  claim_attempts.append(path)
  if path=="handshake":
   return {"accepted":True,"connector_id":"workbuddy-personal"}
  raise RuntimeError("Hub HTTP 503: unavailable")
 monkeypatch.setattr(server,"_post",fail_claim_after_handshake)
 result=server.call_tool("workbuddy_claim_task",{})
 assert result["isError"] is True
 assert claim_attempts==["handshake","claim"]



@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_idempotent_retry_accepts_only_transient_server_statuses(monkeypatch, status):
 attempts = []
 def flaky_post(path, payload):
  attempts.append((path, payload.copy()))
  if len(attempts) == 1:
   raise RuntimeError(f"Hub HTTP {status}: temporary")
  return {"ok": True}
 monkeypatch.setattr(server, "_post", flaky_post)
 monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
 payload = {"sequence": 3, "message": "stable"}
 assert server._post_idempotent("tasks/task-1/progress", payload) == {"ok": True}
 assert len(attempts) == 2
 assert attempts[0] == attempts[1] == ("tasks/task-1/progress", payload)


def test_idempotent_retry_stops_after_three_total_attempts(monkeypatch):
 attempts = []
 def unavailable(path, payload):
  attempts.append((path, payload.copy()))
  raise RuntimeError("Hub HTTP 503: still unavailable")
 monkeypatch.setattr(server, "_post", unavailable)
 monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
 payload = {"result_key": "task-1:stable-result", "state": "completed", "output": "done"}
 with pytest.raises(RuntimeError, match="503"):
  server._post_idempotent("tasks/task-1/result", payload)
 assert len(attempts) == 3
 assert attempts[0] == attempts[1] == attempts[2]


def test_claim_performs_handshake_once_before_claim(monkeypatch):
    calls = []
    task = {"id": "task-hs", "agentId": "codex", "status": {"state": "submitted"}, "history": []}

    def fake_post(path, payload):
        calls.append((path, payload))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {"claimed": True, "task": task, "lease": {
            "lease_token": "private-lease-material", "lease_version": 1,
            "lease_expires_at": time.time() + 120,
        }}

    monkeypatch.setattr(server, "_post", fake_post)
    assert server.tool_claim({})["isError"] is False
    assert [path for path, _ in calls] == ["handshake", "claim"]
    assert calls[0][1]["protocol_version"] == "1.0"
    assert "claim" in calls[0][1]["capabilities"]
    assert server.HANDSHAKE_READY is True


def test_handshake_identity_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(server, "_post", lambda _path, _payload: {
        "accepted": True, "connector_id": "attacker-connector",
    })
    result = server.call_tool("workbuddy_claim_task", {})
    assert result["isError"] is True
    assert "identity" in result["content"][0]["text"]


def test_connector_id_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("A2A_WORKBUDDY_CONNECTOR_ID", "../other")
    with pytest.raises(RuntimeError, match="格式无效"):
        server._connector_id()


def test_heartbeat_requires_handshake_and_sends_configured_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append((path, payload)) or (
        {"accepted": True, "connector_id": "workbuddy-personal"} if path == "handshake"
        else {"accepted": True, "receipt_id": payload["heartbeat_id"]}
    ))
    result = server.tool_heartbeat({"status": "busy", "current_task_id": "task-123"})
    assert result["isError"] is False
    assert [path for path, _ in calls] == ["handshake", "heartbeat"]
    assert calls[1][1]["connector_id"] == "workbuddy-personal"
    assert calls[1][1]["status"] == "busy"
    assert calls[1][1]["current_task_id"] == "task-123"
    assert "lease_token" not in result["content"][0]["text"]


def test_status_uses_authenticated_get_after_handshake(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(("POST", path)) or {
        "accepted": True, "connector_id": "workbuddy-personal",
    })
    monkeypatch.setattr(server, "_get", lambda path: calls.append(("GET", path)) or {
        "connector_id": "workbuddy-personal", "online": True, "status": "idle",
    })
    result = server.tool_status({})
    assert result["isError"] is False
    assert calls == [("POST", "handshake"), ("GET", "presence")]
    assert "online" in result["content"][0]["text"]


def test_heartbeat_rejects_invalid_status_before_network(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {
        "accepted": True, "connector_id": "workbuddy-personal",
    })
    result = server.call_tool("workbuddy_heartbeat", {"status": "root"})
    assert result["isError"] is True
    assert calls == []


def test_connection_check_is_task_free_and_returns_redacted_receipts(monkeypatch):
    calls = []
    def fake_post(path, payload):
        calls.append(("POST", path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "heartbeat":
            return {"accepted": True, "receipt_id": payload["heartbeat_id"], "server_time": "2026-11-23T10:00:00Z"}
        raise AssertionError(f"unexpected POST {path}")
    def fake_get(path):
        calls.append(("GET", path, {}))
        return {"connector_id": "workbuddy-personal", "online": True, "server_time": "2026-11-23T10:00:00Z"}
    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)
    result = server.call_tool("workbuddy_connection_check", {})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["connection_check"] == "passed"
    assert payload["handshake_accepted"] is True
    assert payload["heartbeat_accepted"] is True
    assert payload["presence_online_after"] is True
    assert payload["task_claimed"] is False
    assert payload["task_executed"] is False
    assert [call[1] for call in calls] == ["handshake", "presence", "heartbeat", "presence"]
    assert calls[2][2]["status"] == "idle"
    assert calls[2][2]["current_task_id"] is None
    assert all("claim" not in call[1] for call in calls)
    assert "lease_token" not in result["content"][0]["text"]


def test_connection_check_fails_closed_when_heartbeat_not_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: (
        {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "handshake" else {"accepted": False}
    ))
    monkeypatch.setattr(server, "_get", lambda _path: {"online": True})
    result = server.call_tool("workbuddy_connection_check", {})
    assert result["isError"] is True
    assert "未确认" in result["content"][0]["text"]


def test_connection_check_trace_has_ordered_timestamps_and_never_fabricates_receipt_ids(monkeypatch):
    calls = []
    def fake_post(path, payload):
        calls.append(("POST", path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "heartbeat":
            return {"accepted": True, "server_time": "2026-11-23T10:00:00Z"}
        raise AssertionError(f"unexpected POST {path}")
    def fake_get(path):
        calls.append(("GET", path, {}))
        return {"connector_id": "workbuddy-personal", "online": True}
    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)
    result = server.call_tool("workbuddy_connection_check", {})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["check_id"].startswith("conn-")
    assert payload["heartbeat_id"].startswith("hb-")
    assert payload["handshake_receipt_id"] is None
    assert payload["heartbeat_receipt_id"] is None
    timestamps = [
        payload["started_at"],
        payload["handshake_observed_at"],
        payload["presence_before_observed_at"],
        payload["heartbeat_observed_at"],
        payload["presence_after_observed_at"],
        payload["completed_at"],
    ]
    from datetime import datetime
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timestamps]
    assert parsed == sorted(parsed)
    assert payload["task_claimed"] is False and payload["task_executed"] is False
    assert [call[1] for call in calls] == ["handshake", "presence", "heartbeat", "presence"]


@pytest.mark.parametrize(
    ("presence", "message"),
    [
        ({"connector_id": "workbuddy-personal", "online": False}, "未确认 online"),
        ({"connector_id": "other-connector", "online": True}, "identity"),
    ],
)
def test_connection_check_fails_closed_when_final_presence_is_not_our_online_connector(
    monkeypatch, presence, message
):
    post_calls = []

    def fake_post(path, payload):
        post_calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        if path == "heartbeat":
            return {"accepted": True, "receipt_id": "hub-receipt-123"}
        raise AssertionError(f"unexpected POST {path}")

    get_calls = []

    def fake_get(path):
        get_calls.append(path)
        return {"connector_id": "workbuddy-personal", "online": True} if len(get_calls) == 1 else presence

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)
    result = server.call_tool("workbuddy_connection_check", {})

    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert post_calls == ["handshake", "heartbeat"]
    assert get_calls == ["presence", "presence"]


def test_cancellation_checkpoint_keeps_lease_when_hub_reports_not_cancelled(monkeypatch):
    calls = []
    with server.LEASES_LOCK:
        server.LEASES["task-check"] = {
            "lease_token": "private-lease", "lease_version": 7,
            "lease_expires_at": time.time() + 120,
        }

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "accepted": True, "connector_id": "workbuddy-personal",
            "current_task_id": "task-check", "cancel_requested": False,
            "cancel_reason": None,
        }

    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_check_task_cancellation", {"task_id": "task-check"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["cancel_requested"] is False
    assert payload["should_stop"] is False
    assert payload["local_lease_discarded"] is False
    assert "task-check" in server.LEASES
    assert [path for path, _ in calls] == ["handshake", "heartbeat"]
    assert calls[-1][1]["status"] == "busy"
    assert calls[-1][1]["current_task_id"] == "task-check"


def test_cancellation_checkpoint_discards_lease_and_blocks_later_progress(monkeypatch):
    calls = []
    with server.LEASES_LOCK:
        server.LEASES["task-cancelled"] = {
            "lease_token": "private-lease", "lease_version": 8,
            "lease_expires_at": time.time() + 120,
        }

    def fake_post(path, payload):
        calls.append((path, payload.copy()))
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "accepted": True, "connector_id": "workbuddy-personal",
            "current_task_id": "task-cancelled", "cancel_requested": True,
            "cancel_reason": "task_cancelled_by_caller",
        }

    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_check_task_cancellation", {"task_id": "task-cancelled"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["should_stop"] is True
    assert payload["cancel_reason"] == "task_cancelled_by_caller"
    assert payload["local_lease_discarded"] is True
    assert "task-cancelled" not in server.LEASES

    progress = server.call_tool("workbuddy_progress_task", {
        "task_id": "task-cancelled", "sequence": 2, "message": "must not send",
    })
    assert progress["isError"] is True
    assert [path for path, _ in calls] == ["handshake", "heartbeat"]


def test_cancellation_checkpoint_requires_local_lease_before_network(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post", lambda path, payload: calls.append(path) or {})
    result = server.call_tool("workbuddy_check_task_cancellation", {"task_id": "not-held"})
    assert result["isError"] is True
    assert "没有该任务的有效租约" in result["content"][0]["text"]
    assert calls == []


def test_cancellation_checkpoint_fails_closed_on_missing_boolean_and_keeps_lease(monkeypatch):
    with server.LEASES_LOCK:
        server.LEASES["task-unknown-cancel"] = {
            "lease_token": "private-lease", "lease_version": 9,
            "lease_expires_at": time.time() + 120,
        }

    def fake_post(path, payload):
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        return {
            "accepted": True, "connector_id": "workbuddy-personal",
            "current_task_id": "task-unknown-cancel",
        }

    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool("workbuddy_check_task_cancellation", {
        "task_id": "task-unknown-cancel",
    })
    assert result["isError"] is True
    assert "cancel_requested" in result["content"][0]["text"]
    assert "task-unknown-cancel" in server.LEASES


def test_cancellation_checkpoint_tool_is_registered_with_required_task_id():
    tool = next(item for item in server.TOOLS if item["name"] == "workbuddy_check_task_cancellation")
    assert tool["inputSchema"]["required"] == ["task_id"]
    assert "workbuddy_check_task_cancellation" in server.TOOL_FUNCS


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("connector_id", "another-connector"),
        ("current_task_id", "another-task"),
    ],
)
def test_cancellation_checkpoint_fails_closed_on_mismatched_receipt(
    monkeypatch, field, wrong_value
):
    task_id = "task-receipt-match"
    with server.LEASES_LOCK:
        server.LEASES[task_id] = {
            "lease_token": "private-lease", "lease_version": 10,
            "lease_expires_at": time.time() + 120,
        }

    calls = []

    def fake_post(path, payload):
        calls.append(path)
        if path == "handshake":
            return {"accepted": True, "connector_id": "workbuddy-personal"}
        receipt = {
            "accepted": True,
            "connector_id": "workbuddy-personal",
            "current_task_id": task_id,
            "cancel_requested": True,
            "cancel_reason": "task_cancelled_by_caller",
        }
        receipt[field] = wrong_value
        return receipt

    monkeypatch.setattr(server, "_post", fake_post)
    result = server.call_tool(
        "workbuddy_check_task_cancellation", {"task_id": task_id}
    )

    assert result["isError"] is True
    assert "身份或任务不匹配" in result["content"][0]["text"]
    assert task_id in server.LEASES
    assert calls == ["handshake", "heartbeat"]
