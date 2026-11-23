#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WorkBuddy stdio MCP bridge for the A2A Hub outbound-pull REST API."""
from __future__ import annotations
import json, os, sys, threading, time, uuid, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED = {"2024-11-05", "2025-03-26", "2025-06-18"}
LEASES: dict[str, dict[str, Any]] = {}
LEASES_LOCK = threading.Lock()
MAX_ACTIVE_LEASES = 1
HANDSHAKE_READY = False
HANDSHAKE_LOCK = threading.Lock()
TOOLS = [
 {"name":"workbuddy_connector_status","description":"与 Hub 完成协议握手并读取连接 presence；online 只表示最近收到认证心跳，不代表 Desktop UI 已启动。","inputSchema":{"type":"object","properties":{},"required":[]}},
 {"name":"workbuddy_heartbeat","description":"向 Hub 发送一次认证心跳。status 可为 idle/busy/degraded；只发送单次心跳，不启动后台轮询。","inputSchema":{"type":"object","properties":{"status":{"type":"string","enum":["idle","busy","degraded"]},"current_task_id":{"type":"string","maxLength":128}},"required":["status"]}},
 {"name":"workbuddy_connection_check","description":"执行一次连接专用检查：握手、读取 presence、发送一次 idle 心跳并复查 presence。绝不领取或执行任务。","inputSchema":{"type":"object","properties":{},"required":[]}},
 {"name":"workbuddy_claim_task","description":"从 A2A Hub Pull 队列领取一项任务。只领取、不执行；返回任务 prompt。每次用户明确要求处理 Hub 任务时调用一次，不要循环轮询。","inputSchema":{"type":"object","properties":{"agent_ids":{"type":"array","items":{"type":"string"},"description":"可选：进一步缩小领取范围，不能扩大服务端白名单"},"lease_seconds":{"type":"integer","minimum":15,"maximum":900}},"required":[]}},
 {"name":"workbuddy_start_task","description":"领取任务后、开始执行前调用。lease token 由 MCP 客户端内部保管，不要索取或输出 token。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}},
 {"name":"workbuddy_renew_task","description":"任务执行时间较长、租约即将到期时续租。仅续当前 MCP 进程领取的任务。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}},
 {"name":"workbuddy_progress_task","description":"向 Hub 回传任务进度。sequence 必须从 1 开始严格递增；重复提交相同 sequence/message 可安全重放。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string"},"sequence":{"type":"integer","minimum":1},"message":{"type":"string","minLength":1,"maxLength":4000}},"required":["task_id","sequence","message"]}},
 {"name":"workbuddy_submit_result","description":"任务执行结束后回传 completed 或 failed 结果。不要回传凭据、token、cookie 或其他秘密。result_key 可省略，省略时客户端使用 task_id 派生稳定幂等键。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string"},"state":{"type":"string","enum":["completed","failed"]},"output":{"type":"string","maxLength":20000},"result_key":{"type":"string","minLength":8,"maxLength":128}},"required":["task_id","state","output"]}}
]

def _write(obj: dict[str, Any]) -> None:
 sys.stdout.write(json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n"); sys.stdout.flush()
def _log(message: str) -> None:
 sys.stderr.write("[a2a-workbuddy-mcp] "+message+"\n"); sys.stderr.flush()
def _config() -> tuple[str,str]:
 base=os.environ.get("A2A_WORKBUDDY_HUB_URL","").strip().rstrip("/")
 token=os.environ.get("A2A_WORKBUDDY_CONNECTOR_TOKEN","")
 if not base: raise RuntimeError("未配置 Hub HTTPS 地址，请在 Connector 配置中填写 A2A_WORKBUDDY_HUB_URL")
 parsed=urllib.parse.urlsplit(base)
 local_http=parsed.scheme=="http" and parsed.hostname in {"localhost","127.0.0.1","::1"}
 if parsed.scheme!="https" and not local_http: raise RuntimeError("Hub 地址必须使用 HTTPS（仅 localhost 测试允许 HTTP）")
 if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment: raise RuntimeError("Hub 地址格式无效")
 if not token or len(token)<32: raise RuntimeError("WorkBuddy Connector Token 未配置或长度不足 32 位")
 return base,token
class _NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self, req, fp, code, msg, headers, newurl): return None

def _post(path: str,payload: dict[str,Any]) -> dict[str,Any]:
 base,token=_config()
 request=urllib.request.Request(base+"/integrations/workbuddy/v1/"+path.lstrip("/"),data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json","Accept":"application/json","X-WorkBuddy-Connector-Token":token},method="POST")
 try:
  with urllib.request.build_opener(_NoRedirect()).open(request,timeout=30) as response:
   raw=response.read(256*1024+1)
   if len(raw)>256*1024: raise RuntimeError("Hub 响应超过 256 KiB 限制")
   data=json.loads(raw.decode("utf-8"))
   if not isinstance(data,dict): raise RuntimeError("Hub 返回了非对象 JSON")
   return data
 except urllib.error.HTTPError as exc:
  try:
   error=json.loads(exc.read(16*1024).decode("utf-8")); detail=error.get("detail","请求被 Hub 拒绝") if isinstance(error,dict) else "请求被 Hub 拒绝"
  except Exception: detail="Hub 返回 HTTP 错误"
  raise RuntimeError(f"Hub HTTP {exc.code}: {detail}") from None
 except urllib.error.URLError as exc: raise RuntimeError(f"无法连接 Hub：{type(exc.reason).__name__}") from None
 except (UnicodeDecodeError,json.JSONDecodeError): raise RuntimeError("Hub 返回了无效 JSON") from None
def _get(path: str) -> dict[str,Any]:
 base,token=_config()
 request=urllib.request.Request(base+"/integrations/workbuddy/v1/"+path.lstrip("/"),headers={"Accept":"application/json","X-WorkBuddy-Connector-Token":token},method="GET")
 try:
  with urllib.request.build_opener(_NoRedirect()).open(request,timeout=15) as response:
   raw=response.read(64*1024+1)
   if len(raw)>64*1024: raise RuntimeError("Hub 响应超过 64 KiB 限制")
   data=json.loads(raw.decode("utf-8"))
   if not isinstance(data,dict): raise RuntimeError("Hub 返回了非对象 JSON")
   return data
 except urllib.error.HTTPError as exc:
  try:
   error=json.loads(exc.read(16*1024).decode("utf-8")); detail=error.get("detail","请求被 Hub 拒绝") if isinstance(error,dict) else "请求被 Hub 拒绝"
  except Exception: detail="Hub 返回 HTTP 错误"
  raise RuntimeError(f"Hub HTTP {exc.code}: {detail}") from None
 except urllib.error.URLError as exc: raise RuntimeError(f"无法连接 Hub：{type(exc.reason).__name__}") from None
 except (UnicodeDecodeError,json.JSONDecodeError): raise RuntimeError("Hub 返回了无效 JSON") from None

def _connector_id() -> str:
 value=os.environ.get("A2A_WORKBUDDY_CONNECTOR_ID","workbuddy-personal").strip()
 if not value or len(value)>64 or not value[0].isalnum() or any(not (ch.isalnum() or ch in "._-") for ch in value):
  raise RuntimeError("A2A_WORKBUDDY_CONNECTOR_ID 格式无效")
 return value

def _ensure_handshake() -> dict[str,Any]:
 global HANDSHAKE_READY
 with HANDSHAKE_LOCK:
  if HANDSHAKE_READY: return {"accepted":True,"cached":True,"connector_id":_connector_id()}
  result=_post("handshake",{"connector_id":_connector_id(),"protocol_version":"1.0","client_version":"1.0.0","capabilities":["claim","start","renew","progress","result","heartbeat"]})
  if result.get("accepted") is not True or result.get("connector_id") != _connector_id():
   raise RuntimeError("Hub handshake 响应与配置的 connector identity 不匹配")
  HANDSHAKE_READY=True
  return result

def tool_status(_args: dict[str,Any]) -> dict[str,Any]:
 _ensure_handshake()
 return _ok(_get("presence"))

def tool_heartbeat(args: dict[str,Any]) -> dict[str,Any]:
 # Reject malformed input locally before performing any network handshake.
 status=args.get("status")
 if status not in {"idle","busy","degraded"}: raise RuntimeError("status 必须是 idle、busy 或 degraded")
 task_id=args.get("current_task_id")
 if task_id is not None and (not isinstance(task_id,str) or len(task_id)>128): raise RuntimeError("current_task_id 无效")
 _ensure_handshake()
 payload={"connector_id":_connector_id(),"heartbeat_id":"hb-"+uuid.uuid4().hex,"status":status,"current_task_id":task_id}
 return _ok(_post("heartbeat",payload))


def _utc_now() -> str:
 return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

def tool_connection_check(_args: dict[str,Any]) -> dict[str,Any]:
 """Perform one task-free connection check with traceable, non-secret receipt metadata."""
 check_id="conn-"+uuid.uuid4().hex
 started_at=_utc_now()
 handshake=_ensure_handshake()
 handshake_observed_at=_utc_now()
 before=_get("presence")
 presence_before_observed_at=_utc_now()
 heartbeat_id="hb-"+uuid.uuid4().hex
 receipt=_post("heartbeat",{"connector_id":_connector_id(),"heartbeat_id":heartbeat_id,"status":"idle","current_task_id":None})
 heartbeat_observed_at=_utc_now()
 if receipt.get("accepted") is not True:
  raise RuntimeError("Hub 未确认连接检查心跳")
 after=_get("presence")
 presence_after_observed_at=_utc_now()
 if after.get("online") is not True:
  raise RuntimeError("Hub 已接受心跳，但连接 presence 未确认 online")
 if after.get("connector_id") != _connector_id():
  raise RuntimeError("Hub presence connector identity 与当前配置不匹配")
 completed_at=_utc_now()
 return _ok({
  "connection_check":"passed",
  "check_id":check_id,
  "started_at":started_at,
  "completed_at":completed_at,
  "connector_id":_connector_id(),
  "handshake_accepted":True,
  "handshake_receipt_id":handshake.get("receipt_id"),
  "handshake_observed_at":handshake_observed_at,
  "presence_online_before":before.get("online") is True,
  "presence_before_observed_at":presence_before_observed_at,
  "heartbeat_accepted":True,
  "heartbeat_id":heartbeat_id,
  "heartbeat_receipt_id":receipt.get("receipt_id"),
  "heartbeat_observed_at":heartbeat_observed_at,
  "presence_online_after":after.get("online") is True,
  "presence_after_observed_at":presence_after_observed_at,
  "hub_server_time":receipt.get("server_time") or after.get("server_time"),
  "task_claimed":False,
  "task_executed":False,
  "note":"时间戳由本地 MCP 进程记录；receipt_id 仅在 Hub 实际返回时提供。该结果不证明 Desktop UI 身份，也未领取或执行任务。"
 })


def _post_idempotent(path: str, payload: dict[str,Any]) -> dict[str,Any]:
 """Retry only operations whose server contract is idempotent.

 Progress is keyed by sequence+message and result by a stable result_key, so
 retrying after a lost response cannot append duplicate progress or results.
 Claim/start/renew are deliberately excluded because they are not replay-safe.
 """
 retryable_prefixes = ("无法连接 Hub：", "Hub HTTP 500:", "Hub HTTP 502:", "Hub HTTP 503:", "Hub HTTP 504:")
 for attempt in range(3):
  try:
   return _post(path, payload)
  except RuntimeError as exc:
   if attempt == 2 or not str(exc).startswith(retryable_prefixes):
    raise
   time.sleep(0.2 * (attempt + 1))
 raise RuntimeError("Hub 请求重试失败")

def _ok(data: Any) -> dict[str,Any]:
 return {"content":[{"type":"text","text":json.dumps(data,ensure_ascii=False,indent=2,default=str)}],"isError":False}
def _err(message: str) -> dict[str,Any]:
 return {"content":[{"type":"text","text":message}],"isError":True}
def _prune_expired_leases() -> None:
 now = time.time()
 with LEASES_LOCK:
  expired = [task_id for task_id, lease in LEASES.items() if float(lease.get("lease_expires_at", 0)) <= now]
  for task_id in expired: LEASES.pop(task_id, None)

def _lease(task_id: str) -> dict[str,Any]:
 _prune_expired_leases()
 with LEASES_LOCK: value=LEASES.get(task_id)
 if not value: raise RuntimeError("当前 MCP 会话没有该任务的有效租约；请等待旧租约到期后重新领取")
 return {"lease_token":value["lease_token"],"lease_version":value["lease_version"]}
def _task_prompt(task: dict[str,Any]) -> str:
 chunks=[]
 for message in task.get("history") or []:
  for part in message.get("parts") or []:
   if part.get("kind")=="text" or "text" in part:
    value=part.get("text")
    if isinstance(value,str): chunks.append(value)
 return "\n".join(chunks).strip()
def tool_claim(args: dict[str,Any]) -> dict[str,Any]:
 _ensure_handshake()
 _prune_expired_leases()
 with LEASES_LOCK:
  if len(LEASES) >= MAX_ACTIVE_LEASES:
   return _err("当前 MCP 会话已有未完成任务；请先提交该任务结果，再领取下一项")
 payload={}
 if args.get("agent_ids") is not None: payload["agent_ids"]=args["agent_ids"]
 if args.get("lease_seconds") is not None: payload["lease_seconds"]=args["lease_seconds"]
 result=_post("claim",payload)
 if not result.get("claimed"): return _ok({"claimed":False,"message":"当前没有可领取的任务"})
 task=result.get("task") or {}; lease=result.get("lease") or {}; task_id=str(task.get("id") or "")
 if not task_id or not lease.get("lease_token") or not lease.get("lease_version"): raise RuntimeError("Hub claim 响应缺少任务或租约字段")
 try: expires_at=float(lease["lease_expires_at"])
 except (KeyError,TypeError,ValueError): raise RuntimeError("Hub claim 响应缺少有效租约过期时间") from None
 if expires_at <= time.time(): raise RuntimeError("Hub 返回的任务租约已经过期")
 with LEASES_LOCK: LEASES[task_id]={"lease_token":str(lease["lease_token"]),"lease_version":int(lease["lease_version"]),"lease_expires_at":expires_at}
 return _ok({"claimed":True,"task_id":task_id,"agent_id":task.get("agentId"),"state":(task.get("status") or {}).get("state"),"prompt":_task_prompt(task),"lease_expires_at":lease.get("lease_expires_at"),"note":"租约凭据由本地 MCP 进程保管，不会返回给模型。"})
def tool_start(args: dict[str,Any]) -> dict[str,Any]:
 task_id=str(args.get("task_id") or "").strip(); return _ok(_post(f"tasks/{urllib.parse.quote(task_id,safe='')}/start",_lease(task_id)))
def tool_renew(args: dict[str,Any]) -> dict[str,Any]:
 task_id=str(args.get("task_id") or "").strip(); payload=_lease(task_id)
 result=_post(f"tasks/{urllib.parse.quote(task_id,safe='')}/renew",payload)
 try: expires_at=float(result["lease_expires_at"])
 except (KeyError,TypeError,ValueError): raise RuntimeError("Hub renew 响应缺少有效租约过期时间") from None
 if expires_at <= time.time(): raise RuntimeError("Hub renew 返回的租约已经过期")
 with LEASES_LOCK:
  if task_id in LEASES: LEASES[task_id]["lease_expires_at"]=expires_at
 return _ok(result)
def tool_progress(args: dict[str,Any]) -> dict[str,Any]:
 task_id=str(args.get("task_id") or "").strip(); payload={**_lease(task_id),"sequence":args.get("sequence"),"message":args.get("message")}
 return _ok(_post_idempotent(f"tasks/{urllib.parse.quote(task_id,safe='')}/progress",payload))
def tool_result(args: dict[str,Any]) -> dict[str,Any]:
 task_id=str(args.get("task_id") or "").strip(); payload={**_lease(task_id),"result_key":args.get("result_key") or (task_id+":workbuddy-result-v1"),"state":args.get("state"),"output":args.get("output","")}
 result=_post_idempotent(f"tasks/{urllib.parse.quote(task_id,safe='')}/result",payload)
 with LEASES_LOCK: LEASES.pop(task_id,None)
 return _ok(result)
TOOL_FUNCS={"workbuddy_connector_status":tool_status,"workbuddy_heartbeat":tool_heartbeat,"workbuddy_connection_check":tool_connection_check,"workbuddy_claim_task":tool_claim,"workbuddy_start_task":tool_start,"workbuddy_renew_task":tool_renew,"workbuddy_progress_task":tool_progress,"workbuddy_submit_result":tool_result}
def call_tool(name: str,args: dict[str,Any]) -> dict[str,Any]:
 fn=TOOL_FUNCS.get(name)
 if fn is None: return _err("未知工具")
 try: return fn(args or {})
 except Exception as exc:
  _log(f"{name} failed: {type(exc).__name__}")
  return _err(f"{type(exc).__name__}: {exc}")
def _handle(message: dict[str,Any]) -> None:
 method=message.get("method"); request_id=message.get("id")
 if request_id is None: return
 params=message.get("params") or {}
 if method=="initialize":
  requested=params.get("protocolVersion")
  _write({"jsonrpc":"2.0","id":request_id,"result":{"protocolVersion":requested if requested in SUPPORTED else PROTOCOL_VERSION,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"a2a-hub-workbuddy-pull","version":"1.0.0"},"instructions":"使用 workbuddy_claim_task 领取一项任务，随后 start；模型执行任务并可用 progress/renew 更新状态，最后 submit_result。只在用户明确要求时领取一次，不要后台轮询。"}})
 elif method=="tools/list": _write({"jsonrpc":"2.0","id":request_id,"result":{"tools":TOOLS}})
 elif method=="tools/call": _write({"jsonrpc":"2.0","id":request_id,"result":call_tool(params.get("name",""),params.get("arguments") or {})})
 elif method=="ping": _write({"jsonrpc":"2.0","id":request_id,"result":{}})
 else: _write({"jsonrpc":"2.0","id":request_id,"error":{"code":-32601,"message":"Method not found"}})
def main() -> None:
 for stream in (sys.stdin,sys.stdout,sys.stderr):
  try: stream.reconfigure(encoding="utf-8",errors="replace")
  except Exception: pass
 _log("stdio MCP bridge started")
 for raw in sys.stdin:
  if not raw.strip(): continue
  try: message=json.loads(raw)
  except json.JSONDecodeError: _log("ignored malformed JSON-RPC input"); continue
  if not isinstance(message,dict): _log("ignored non-object JSON-RPC input"); continue
  try: _handle(message)
  except Exception as exc:
   _log(f"protocol handler failed: {type(exc).__name__}")
   if message.get("id") is not None: _write({"jsonrpc":"2.0","id":message["id"],"error":{"code":-32603,"message":"Internal MCP error"}})
if __name__=="__main__": main()
