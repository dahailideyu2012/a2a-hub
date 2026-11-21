from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path
import pytest
import workbuddy_mcp_server as server
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
 with server.LEASES_LOCK: server.LEASES.clear()
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
 monkeypatch.setattr(server,"_post",lambda path,payload:{"claimed":True,"task":task,"lease":{"lease_token":"SECRET-LEASE-MATERIAL","lease_version":2,"lease_expires_at":time.time()+120}})
 result=server.tool_claim({}); text=result["content"][0]["text"]
 assert "请审查代码" in text and "task-1" in text
 assert "SECRET-LEASE-MATERIAL" not in text
 assert server.LEASES["task-1"]["lease_version"]==2
def test_lifecycle_uses_private_lease_and_stable_result_key(monkeypatch):
 calls=[]
 monkeypatch.setattr(server,"_post",lambda path,payload:calls.append((path,payload)) or ({"lease_expires_at":time.time()+120} if path.endswith("/renew") else {"ok":True}))
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
 monkeypatch.setattr(server,"_post",lambda path,payload:{"claimed":False})
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
 monkeypatch.setattr(server,"_post",lambda path,payload:calls.append(path) or {
  "claimed":True,"task":task,"lease":{"lease_token":"private-lease-material","lease_version":1,"lease_expires_at":time.time()+120}
 })
 first=server.tool_claim({})
 second=server.tool_claim({})
 assert first["isError"] is False
 assert second["isError"] is True
 assert len(calls)==1
 assert "已有未完成任务" in second["content"][0]["text"]


def test_expired_local_lease_is_pruned_before_next_claim(monkeypatch):
 task={"id":"task-next","agentId":"codex","status":{"state":"submitted"},"history":[{"parts":[{"kind":"text","text":"next"}]}]}
 with server.LEASES_LOCK:
  server.LEASES["task-expired"]={"lease_token":"old-secret","lease_version":1,"lease_expires_at":time.time()-1}
 monkeypatch.setattr(server,"_post",lambda path,payload:{
  "claimed":True,"task":task,"lease":{"lease_token":"new-secret-material","lease_version":1,"lease_expires_at":time.time()+120}
 })
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
 monkeypatch.setattr(server,"_post",lambda path,payload:claim_attempts.append(path) or (_ for _ in ()).throw(RuntimeError("Hub HTTP 503: unavailable")))
 result=server.tool_claim({})
 assert result["isError"] is True
 assert claim_attempts==["claim"]
