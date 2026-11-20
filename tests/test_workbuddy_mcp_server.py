from __future__ import annotations
import json, os, subprocess, sys
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
 monkeypatch.setattr(server,"_post",lambda path,payload:{"claimed":True,"task":task,"lease":{"lease_token":"SECRET-LEASE-MATERIAL","lease_version":2,"lease_expires_at":12345}})
 result=server.tool_claim({}); text=result["content"][0]["text"]
 assert "请审查代码" in text and "task-1" in text
 assert "SECRET-LEASE-MATERIAL" not in text
 assert server.LEASES["task-1"]["lease_version"]==2
def test_lifecycle_uses_private_lease_and_stable_result_key(monkeypatch):
 calls=[]
 monkeypatch.setattr(server,"_post",lambda path,payload:calls.append((path,payload)) or {"ok":True})
 with server.LEASES_LOCK: server.LEASES["task-2"]={"lease_token":"private-lease","lease_version":3}
 assert not server.tool_start({"task_id":"task-2"})["isError"]
 assert not server.tool_renew({"task_id":"task-2"})["isError"]
 assert not server.tool_progress({"task_id":"task-2","sequence":1,"message":"开始检查"})["isError"]
 assert not server.tool_result({"task_id":"task-2","state":"completed","output":"PASS"})["isError"]
 assert [c[0] for c in calls]==["tasks/task-2/start","tasks/task-2/renew","tasks/task-2/progress","tasks/task-2/result"]
 assert calls[-1][1]["result_key"]=="task-2:workbuddy-result-v1"
 assert calls[-1][1]["lease_token"]=="private-lease"
 assert "task-2" not in server.LEASES
def test_task_lifecycle_rejects_unknown_lease():
 result=server.tool_start({"task_id":"not-claimed"})
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
