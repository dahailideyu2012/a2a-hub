#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WorkBuddy stdio MCP bridge for the A2A Hub outbound-pull REST API."""
from __future__ import annotations
import atexit, hashlib, json, math, os, re, sys, threading, time, uuid, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED = {"2024-11-05", "2025-03-26", "2025-06-18"}
LEASES: dict[str, dict[str, Any]] = {}
LEASES_LOCK = threading.Lock()
CLAIM_LOCK = threading.Lock()
MAX_ACTIVE_LEASES = 1
HANDSHAKE_READY = False
HANDSHAKE_CACHE_KEY: tuple[str, str, str] | None = None
HANDSHAKE_LOCK = threading.Lock()
RECOVERY_SNAPSHOT_LOCK = threading.Lock()
LAST_RECOVERY_SNAPSHOT: tuple[tuple[str, str, str], str] | None = None
TOOLS = [
 {"name":"workbuddy_connector_status","description":"与 Hub 完成协议握手并读取连接 presence；online 只表示最近收到认证心跳，不代表 Desktop UI 已启动。","inputSchema":{"type":"object","properties":{},"required":[]}},
 {"name":"workbuddy_heartbeat","description":"向 Hub 发送一次认证心跳。status 可为 idle/busy/degraded；只发送单次心跳，不启动后台轮询。","inputSchema":{"type":"object","properties":{"status":{"type":"string","enum":["idle","busy","degraded"]},"current_task_id":{"type":"string","minLength":1,"maxLength":128}},"required":["status"]}},
 {"name":"workbuddy_check_task_cancellation","description":"检查当前 MCP 会话持有的任务是否已被取消。执行一次认证 busy 心跳；收到取消信号时立即丢弃本地租约并返回 should_stop=true。适合长任务在子步骤之间主动检查；不启动后台轮询或中断正在运行的外部程序。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string","minLength":1,"maxLength":128}},"required":["task_id"]}},
 {"name":"workbuddy_connection_check","description":"执行一次连接专用检查：握手、读取 presence、发送一次 idle 心跳并复查 presence。绝不领取或执行任务。","inputSchema":{"type":"object","properties":{},"required":[]}},
 {"name":"workbuddy_recovery_diagnostics","description":"只读读取 Hub 当前恢复诊断和 recovery_snapshot_id；不触发握手、心跳、重连或任务操作。用于在用户指示下记录人工处理决定。","inputSchema":{"type":"object","properties":{},"required":[]}},
 {"name":"workbuddy_record_recovery_decision","description":"仅在用户/操作员明确决定后，记录当前诊断快照的 acknowledged、deferred 或 dismissed 决定。必须先读取 workbuddy_recovery_diagnostics；过期快照会被拒绝。本工具只记录回执，绝不执行 reconnect、heartbeat probe、claim 或任务。","inputSchema":{"type":"object","properties":{"snapshot_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"disposition":{"type":"string","enum":["acknowledged","deferred","dismissed"]}},"required":["snapshot_id","disposition"]}},
 {"name":"workbuddy_claim_task","description":"从 A2A Hub Pull 队列领取一项任务。只领取、不执行；返回任务 prompt。每次用户明确要求处理 Hub 任务时调用一次，不要循环轮询。","inputSchema":{"type":"object","properties":{"agent_ids":{"type":"array","minItems":1,"maxItems":32,"uniqueItems":True,"items":{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"description":"可选：进一步缩小领取范围，不能扩大服务端白名单"},"lease_seconds":{"type":"integer","minimum":15,"maximum":900}},"required":[]}},
 {"name":"workbuddy_start_task","description":"领取任务后、开始执行前调用。lease token 由 MCP 客户端内部保管，不要索取或输出 token。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string","minLength":1,"maxLength":128}},"required":["task_id"]}},
 {"name":"workbuddy_renew_task","description":"任务执行时间较长、租约即将到期时续租。仅续当前 MCP 进程领取的任务。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string","minLength":1,"maxLength":128}},"required":["task_id"]}},
 {"name":"workbuddy_progress_task","description":"向 Hub 回传任务进度。sequence 必须从 1 开始严格递增；重复提交相同 sequence/message 可安全重放。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string","minLength":1,"maxLength":128},"sequence":{"type":"integer","minimum":1},"message":{"type":"string","minLength":1,"maxLength":4000}},"required":["task_id","sequence","message"]}},
 {"name":"workbuddy_submit_result","description":"任务执行结束后回传 completed 或 failed 结果。不要回传凭据、token、cookie 或其他秘密。result_key 可省略，省略时客户端使用 task_id 派生稳定幂等键。","inputSchema":{"type":"object","properties":{"task_id":{"type":"string","minLength":1,"maxLength":128},"state":{"type":"string","enum":["completed","failed"]},"output":{"type":"string","maxLength":20000},"result_key":{"type":"string","minLength":8,"maxLength":128}},"required":["task_id","state","output"]}}
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
def _http_timeout(env_name: str, default: float, maximum: float) -> float:
 """Read a bounded per-request timeout; invalid values fail closed."""
 raw = os.environ.get(env_name)
 if raw is None or not raw.strip():
  return default
 try:
  value = float(raw)
 except (TypeError, ValueError):
  raise RuntimeError(f"{env_name} 必须是 1 到 {maximum:g} 秒之间的数字") from None
 if not math.isfinite(value) or value < 1 or value > maximum:
  raise RuntimeError(f"{env_name} 必须是 1 到 {maximum:g} 秒之间的有限数字")
 return value


def _read_limited(response: Any, limit: int, label: str) -> bytes:
 """Read a response to EOF in bounded chunks, tolerating short reads."""
 if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
  raise ValueError("response byte limit must be a non-negative integer")
 chunks: list[bytes] = []
 total = 0
 while True:
  chunk = response.read(min(16 * 1024, limit + 1 - total))
  if chunk == b"":
   break
  if not isinstance(chunk, (bytes, bytearray)):
   raise RuntimeError(f"{label}不是有效的字节流")
  total += len(chunk)
  if total > limit:
   raise RuntimeError(f"{label}超过 {limit} 字节限制")
  chunks.append(bytes(chunk))
 return b"".join(chunks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self, req, fp, code, msg, headers, newurl): return None

def _http_error_detail(exc: urllib.error.HTTPError) -> str:
 """Read a bounded HTTP error body and always release its response stream."""
 try:
  try:
   error=json.loads(_read_limited(exc,16*1024,"Hub 错误响应").decode("utf-8"))
   return error.get("detail","请求被 Hub 拒绝") if isinstance(error,dict) else "请求被 Hub 拒绝"
  except Exception:
   return "Hub 返回 HTTP 错误"
 finally:
  # HTTPError is a wrapper; close its underlying response stream explicitly.
  response_stream = getattr(exc, "fp", None)
  close_stream = getattr(response_stream, "close", None)
  if callable(close_stream):
   try:
    close_stream()
   except Exception:
    # Preserve the original HTTP status even if a broken stream fails to close.
    pass


def _post(path: str,payload: dict[str,Any]) -> dict[str,Any]:
 base,token=_config()
 request=urllib.request.Request(base+"/integrations/workbuddy/v1/"+path.lstrip("/"),data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json","Accept":"application/json","X-WorkBuddy-Connector-Token":token},method="POST")
 try:
  with urllib.request.build_opener(_NoRedirect()).open(request,timeout=_http_timeout("A2A_WORKBUDDY_POST_TIMEOUT_SECONDS", 30, 120)) as response:
   raw=_read_limited(response,256*1024,"Hub 响应")
   data=json.loads(raw.decode("utf-8"))
   if not isinstance(data,dict): raise RuntimeError("Hub 返回了非对象 JSON")
   return data
 except urllib.error.HTTPError as exc:
  detail=_http_error_detail(exc)
  raise RuntimeError(f"Hub HTTP {exc.code}: {detail}") from None
 except urllib.error.URLError as exc: raise RuntimeError(f"无法连接 Hub：{type(exc.reason).__name__}") from None
 except (UnicodeDecodeError,json.JSONDecodeError): raise RuntimeError("Hub 返回了无效 JSON") from None
def _get(path: str) -> dict[str,Any]:
 base,token=_config()
 request=urllib.request.Request(base+"/integrations/workbuddy/v1/"+path.lstrip("/"),headers={"Accept":"application/json","X-WorkBuddy-Connector-Token":token},method="GET")
 try:
  with urllib.request.build_opener(_NoRedirect()).open(request,timeout=_http_timeout("A2A_WORKBUDDY_GET_TIMEOUT_SECONDS", 15, 60)) as response:
   raw=_read_limited(response,64*1024,"Hub 响应")
   data=json.loads(raw.decode("utf-8"))
   if not isinstance(data,dict): raise RuntimeError("Hub 返回了非对象 JSON")
   return data
 except urllib.error.HTTPError as exc:
  detail=_http_error_detail(exc)
  raise RuntimeError(f"Hub HTTP {exc.code}: {detail}") from None
 except urllib.error.URLError as exc: raise RuntimeError(f"无法连接 Hub：{type(exc.reason).__name__}") from None
 except (UnicodeDecodeError,json.JSONDecodeError): raise RuntimeError("Hub 返回了无效 JSON") from None

def _connector_id() -> str:
 value=os.environ.get("A2A_WORKBUDDY_CONNECTOR_ID","workbuddy-personal").strip()
 if not value or len(value)>64 or not value[0].isalnum() or any(not (ch.isalnum() or ch in "._-") for ch in value):
  raise RuntimeError("A2A_WORKBUDDY_CONNECTOR_ID 格式无效")
 return value

def _handshake_cache_key() -> tuple[str, str, str]:
 base, token = _config()
 token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
 return base, _connector_id(), token_fingerprint


def _invalidate_handshake() -> None:
 global HANDSHAKE_READY, HANDSHAKE_CACHE_KEY, LAST_RECOVERY_SNAPSHOT
 with HANDSHAKE_LOCK:
  HANDSHAKE_READY = False
  HANDSHAKE_CACHE_KEY = None
 with RECOVERY_SNAPSHOT_LOCK:
  LAST_RECOVERY_SNAPSHOT = None


def _ensure_handshake() -> dict[str,Any]:
 global HANDSHAKE_READY, HANDSHAKE_CACHE_KEY
 with HANDSHAKE_LOCK:
  cache_key = _handshake_cache_key()
  if HANDSHAKE_READY and HANDSHAKE_CACHE_KEY == cache_key:
   return {"accepted":True,"cached":True,"connector_id":cache_key[1]}
  HANDSHAKE_READY = False
  HANDSHAKE_CACHE_KEY = None
  result=_post("handshake",{"connector_id":cache_key[1],"protocol_version":"1.0","client_version":"1.0.0","capabilities":["claim","start","renew","progress","result","heartbeat"]})
  if not isinstance(result, dict) or result.get("accepted") is not True or result.get("connector_id") != cache_key[1]:
   raise RuntimeError("Hub handshake 响应与配置的 connector identity 不匹配")
  HANDSHAKE_CACHE_KEY = cache_key
  HANDSHAKE_READY = True
  return result


def _validate_heartbeat_receipt(result: dict[str,Any], payload: dict[str,Any]) -> dict[str,Any]:
 if not isinstance(result, dict) or result.get("accepted") is not True:
  raise RuntimeError("Hub 未确认连接心跳")
 if result.get("connector_id") != payload["connector_id"]:
  raise RuntimeError("Hub 心跳回执 connector identity 不匹配")
 if result.get("status") != payload["status"] or result.get("current_task_id") != payload.get("current_task_id"):
  raise RuntimeError("Hub 心跳回执状态或任务不匹配")
 receipt_id = result.get("receipt_id")
 if receipt_id is not None and receipt_id != payload["heartbeat_id"]:
  raise RuntimeError("Hub 心跳回执 ID 不匹配")
 if not isinstance(result.get("cancel_requested"), bool):
  raise RuntimeError("Hub 心跳回执缺少有效 cancel_requested 布尔值")
 reason = result.get("cancel_reason")
 if reason is not None and not isinstance(reason, str):
  raise RuntimeError("Hub 心跳回执 cancel_reason 格式无效")
 return result


def _post_heartbeat(payload: dict[str,Any]) -> dict[str,Any]:
 """Send one heartbeat and invalidate cached handshake on auth/protocol rejection."""
 try:
  return _post("heartbeat", payload)
 except RuntimeError as exc:
  if str(exc).startswith(("Hub HTTP 401:", "Hub HTTP 403:", "Hub HTTP 409:", "Hub HTTP 426:")):
   _invalidate_handshake()
  raise


def _presence_issue(result: Any, connector_id: str, require_online: bool = False) -> str | None:
 """Return a stable validation code for a presence response without leaking its contents."""
 if not isinstance(result, dict):
  return "shape"
 if result.get("connector_id") != connector_id:
  return "identity"
 if not isinstance(result.get("online"), bool):
  return "online_type"
 if require_online and result.get("online") is not True:
  return "offline"
 return None

def tool_status(_args: dict[str,Any]) -> dict[str,Any]:
 _ensure_handshake()
 result = _get("presence")
 issue = _presence_issue(result, _connector_id())
 if issue == "identity":
  _invalidate_handshake()
  raise RuntimeError("Hub presence connector identity mismatch")
 if issue == "shape":
  raise RuntimeError("Hub presence response must be an object")
 if issue == "online_type":
  raise RuntimeError("Hub presence response lacks a valid online boolean")
 return _ok(result)

def tool_recovery_diagnostics(_args: dict[str,Any]) -> dict[str,Any]:
 global LAST_RECOVERY_SNAPSHOT
 # Diagnostics must not handshake: a handshake intentionally clears prior error/retry
 # history, which would hide the state this read-only tool is meant to inspect.
 result=_get("diagnostics")
 if not isinstance(result,dict):
  raise RuntimeError("Hub diagnostics response must be an object")
 connector_id=_connector_id()
 if result.get("connector_id") != connector_id:
  raise RuntimeError("Hub diagnostics connector identity mismatch")
 snapshot_id=result.get("recovery_snapshot_id")
 if not isinstance(snapshot_id,str) or not re.fullmatch(r"[0-9a-f]{64}",snapshot_id):
  raise RuntimeError("Hub diagnostics response lacks a valid recovery snapshot ID")
 if result.get("diagnostics_version") != 4:
  raise RuntimeError("Hub diagnostics version 4 is required for recovery decisions")
 recovery=result.get("recovery")
 if not isinstance(recovery,dict) or not isinstance(recovery.get("action"),str):
  raise RuntimeError("Hub diagnostics recovery section is invalid")
 if recovery.get("automatic_actions_enabled") is not False:
  raise RuntimeError("Hub diagnostics must confirm automatic recovery actions are disabled")
 operator_decision=result.get("operator_decision")
 if operator_decision is not None and not isinstance(operator_decision,dict):
  raise RuntimeError("Hub diagnostics operator decision section is invalid")
 snapshot_key=_handshake_cache_key()
 with RECOVERY_SNAPSHOT_LOCK:
  LAST_RECOVERY_SNAPSHOT=(snapshot_key,snapshot_id)
 return _ok(result)


def tool_record_recovery_decision(args: dict[str,Any]) -> dict[str,Any]:
 global LAST_RECOVERY_SNAPSHOT
 snapshot_id=args.get("snapshot_id")
 disposition=args.get("disposition")
 if not isinstance(snapshot_id,str) or not re.fullmatch(r"[0-9a-f]{64}",snapshot_id):
  raise RuntimeError("snapshot_id 必须是 64 位小写十六进制")
 if disposition not in {"acknowledged","deferred","dismissed"}:
  raise RuntimeError("disposition 必须是 acknowledged、deferred 或 dismissed")
 snapshot_key=_handshake_cache_key()
 with RECOVERY_SNAPSHOT_LOCK:
  current=LAST_RECOVERY_SNAPSHOT
  if current is None or current != (snapshot_key,snapshot_id):
   raise RuntimeError("必须先读取当前 connector 的恢复诊断快照；快照缺失或已过期")
  # Consume before the write: if the response is lost, re-read diagnostics before retrying.
  LAST_RECOVERY_SNAPSHOT=None
 # This endpoint records only an operator decision. It does not handshake or
 # perform the recommended recovery action, and it never touches task APIs.
 result=_post("recovery/decision",{
  "connector_id":_connector_id(),
  "snapshot_id":snapshot_id,
  "disposition":disposition,
 })
 if (result.get("accepted") is not True
     or result.get("connector_id") != _connector_id()
     or result.get("snapshot_id") != snapshot_id
     or result.get("disposition") != disposition
     or result.get("action_executed") is not False
     or result.get("automatic_actions_enabled") is not False):
  raise RuntimeError("Hub recovery decision receipt failed closed validation")
 return _ok(result)


def tool_heartbeat(args: dict[str,Any]) -> dict[str,Any]:
 # Reject malformed input locally before performing any network handshake.
 status=args.get("status")
 if status not in {"idle","busy","degraded"}: raise RuntimeError("status 必须是 idle、busy 或 degraded")
 raw_task_id=args.get("current_task_id")
 task_id = _validated_task_id_value(raw_task_id, "current_task_id") if raw_task_id is not None else None
 if status == "idle" and task_id is not None:
  raise RuntimeError("idle 心跳不能绑定 current_task_id")
 if status == "busy" and task_id is None:
  raise RuntimeError("busy 心跳必须绑定有效 current_task_id")
 _ensure_handshake()
 payload={"connector_id":_connector_id(),"heartbeat_id":"hb-"+uuid.uuid4().hex,"status":status,"current_task_id":task_id}
 return _ok(_validate_heartbeat_receipt(_post_heartbeat(payload), payload))



def tool_check_task_cancellation(args: dict[str,Any]) -> dict[str,Any]:
 """Check a locally leased task's durable cancellation flag with one heartbeat."""
 task_id = _validated_task_id(args)
 # A model cannot use this tool to probe arbitrary task IDs: the bridge must
 # already hold the corresponding private lease in this MCP process.
 _lease(task_id)
 _ensure_handshake()
 heartbeat_id="hb-"+uuid.uuid4().hex
 heartbeat_payload={
  "connector_id":_connector_id(),
  "heartbeat_id":heartbeat_id,
  "status":"busy",
  "current_task_id":task_id,
 }
 response=_validate_heartbeat_receipt(_post_heartbeat(heartbeat_payload),heartbeat_payload)
 cancel_requested=response["cancel_requested"]
 reason=response.get("cancel_reason")
 discarded=False
 if cancel_requested:
  # The task is already terminal on the Hub. Dropping the local lease prevents
  # this MCP session from accidentally sending later progress/result mutations.
  with LEASES_LOCK:
   discarded=LEASES.pop(task_id,None) is not None
 return _ok({
  "task_id":task_id,
  "cancel_requested":cancel_requested,
  "should_stop":cancel_requested,
  "cancel_reason":reason if cancel_requested else None,
  "local_lease_discarded":discarded,
  "heartbeat_id":heartbeat_id,
  "note":"这是一次显式协作式检查，不是后台轮询或进程级强制中断。should_stop=true 时应立即停止当前任务，不再提交进度或结果。",
 })


def _utc_now() -> str:
 return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

def _connection_check_failure(
 stage: str, check_id: str, started_at: str, observed: dict[str,str], reason: str
) -> RuntimeError:
 trace = {
  "check_id": check_id,
  "started_at": started_at,
  "failed_stage": stage,
  **observed,
 }
 # Only locally generated identifiers/timestamps are included; never include
 # Hub response bodies, request headers, tokens, or arbitrary exception text.
 return RuntimeError(
  f"{reason} [connection_check_trace={json.dumps(trace,ensure_ascii=False,separators=(',',':'))}]"
 )

def tool_connection_check(_args: dict[str,Any]) -> dict[str,Any]:
 """Perform one task-free connection check with traceable, non-secret receipt metadata."""
 check_id="conn-"+uuid.uuid4().hex
 started_at=_utc_now()
 observed: dict[str,str] = {}
 try:
  handshake=_ensure_handshake()
 except Exception:
  raise _connection_check_failure("handshake",check_id,started_at,observed,"Hub 握手失败") from None
 handshake_observed_at=_utc_now()
 observed["handshake_observed_at"]=handshake_observed_at
 try:
  before=_get("presence")
 except Exception:
  raise _connection_check_failure("presence_before",check_id,started_at,observed,"读取初始 presence 失败") from None
 presence_before_observed_at=_utc_now()
 observed["presence_before_observed_at"]=presence_before_observed_at
 presence_issue = _presence_issue(before,_connector_id())
 presence_failure = {
  "shape": ("presence_before_shape","初始 presence 响应格式无效"),
  "identity": ("presence_before_identity","初始 presence connector identity 不匹配"),
  "online_type": ("presence_before_online","初始 presence 缺少有效 online 布尔值"),
 }
 if presence_issue:
  stage,reason=presence_failure[presence_issue]
  raise _connection_check_failure(stage,check_id,started_at,observed,reason) from None
 heartbeat_id="hb-"+uuid.uuid4().hex
 heartbeat_payload={"connector_id":_connector_id(),"heartbeat_id":heartbeat_id,"status":"idle","current_task_id":None}
 try:
  receipt=_post_heartbeat(heartbeat_payload)
 except Exception:
  raise _connection_check_failure("heartbeat_request",check_id,started_at,observed,"发送连接检查心跳失败") from None
 heartbeat_observed_at=_utc_now()
 observed["heartbeat_observed_at"]=heartbeat_observed_at
 try:
  _validate_heartbeat_receipt(receipt,heartbeat_payload)
 except RuntimeError as exc:
  if str(exc) == "Hub 未确认连接心跳":
   raise _connection_check_failure("heartbeat_acceptance",check_id,started_at,observed,"Hub 未确认连接检查心跳") from None
  raise _connection_check_failure("heartbeat_receipt",check_id,started_at,observed,"Hub 心跳回执校验失败") from None
 try:
  after=_get("presence")
 except Exception:
  raise _connection_check_failure("presence_after",check_id,started_at,observed,"读取最终 presence 失败") from None
 presence_after_observed_at=_utc_now()
 observed["presence_after_observed_at"]=presence_after_observed_at
 presence_issue = _presence_issue(after,_connector_id(),require_online=True)
 presence_failure = {
  "shape": ("final_presence_shape","最终 presence 响应格式无效"),
  "identity": ("final_presence_identity","Hub presence connector identity 与当前配置不匹配"),
  "online_type": ("final_presence_online","Hub presence 缺少有效 online 布尔值"),
  "offline": ("final_presence_online","Hub 已接受心跳，但连接 presence 未确认 online"),
 }
 if presence_issue:
  stage,reason=presence_failure[presence_issue]
  raise _connection_check_failure(stage,check_id,started_at,observed,reason) from None
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
def _cleanup_local_session() -> None:
 """Discard process-local credentials and handshake state during shutdown."""
 with LEASES_LOCK:
  LEASES.clear()
 _invalidate_handshake()


atexit.register(_cleanup_local_session)


def _prune_expired_leases() -> None:
 """Remove expired or structurally invalid local lease records without raising."""
 now = time.time()
 with LEASES_LOCK:
  invalid: list[str] = []
  for task_id, lease in LEASES.items():
   if not isinstance(lease, dict):
    invalid.append(task_id)
    continue
   token = lease.get("lease_token")
   version = lease.get("lease_version")
   expires_at = lease.get("lease_expires_at")
   if (not isinstance(token, str) or not token or len(token) > 4096
       or isinstance(version, bool) or not isinstance(version, int) or version <= 0
       or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))):
    invalid.append(task_id)
    continue
   try:
    expiry = float(expires_at)
   except (OverflowError, TypeError, ValueError):
    invalid.append(task_id)
    continue
   if not math.isfinite(expiry) or expiry <= now:
    invalid.append(task_id)
  for task_id in invalid:
   LEASES.pop(task_id, None)


def _lease(task_id: str) -> dict[str,Any]:
 _prune_expired_leases()
 with LEASES_LOCK:
  value = LEASES.get(task_id)
  if not isinstance(value, dict):
   value = None
  else:
   value = dict(value)
 if value is None:
  raise RuntimeError("当前 MCP 会话没有该任务的有效租约；请等待旧租约到期后重新领取")
 return {"lease_token":value["lease_token"],"lease_version":value["lease_version"]}
def _task_prompt(task: dict[str,Any]) -> str:
 chunks=[]
 for message in task.get("history") or []:
  for part in message.get("parts") or []:
   if not isinstance(part, dict):
    continue
   if part.get("kind")=="text" or "text" in part:
    value=part.get("text")
    if isinstance(value,str): chunks.append(value)
 return "\n".join(chunks).strip()
def _validated_claim_args(args: dict[str,Any]) -> dict[str,Any]:
 """Validate optional claim filters locally before handshake or claim network I/O."""
 payload: dict[str,Any] = {}
 agent_ids = args.get("agent_ids")
 if agent_ids is not None:
  if not isinstance(agent_ids, list) or not 1 <= len(agent_ids) <= 32:
   raise RuntimeError("agent_ids 必须是 1 到 32 项的数组")
  if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", item) for item in agent_ids):
   raise RuntimeError("agent_ids 包含无效 Agent ID")
  if len(set(agent_ids)) != len(agent_ids):
   raise RuntimeError("agent_ids 不能包含重复项")
  payload["agent_ids"] = list(agent_ids)
 lease_seconds = args.get("lease_seconds")
 if lease_seconds is not None:
  if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 15 <= lease_seconds <= 900:
   raise RuntimeError("lease_seconds 必须是 15 到 900 之间的整数")
  payload["lease_seconds"] = lease_seconds
 return payload


def tool_claim(args: dict[str,Any]) -> dict[str,Any]:
 payload = _validated_claim_args(args)
 _ensure_handshake()
 # Serialize the full check -> remote claim -> local registration sequence.
 # Never hold LEASES_LOCK while waiting on the network.
 with CLAIM_LOCK:
  _prune_expired_leases()
  with LEASES_LOCK:
   if len(LEASES) >= MAX_ACTIVE_LEASES:
    return _err("当前 MCP 会话已有未完成任务；请先提交该任务结果，再领取下一项")
  result=_post("claim",payload)
  if not isinstance(result,dict):
   raise RuntimeError("Hub claim 响应格式无效")
  if result.get("claimed") is not True:
   if result.get("claimed") is False:
    return _ok({"claimed":False,"message":"当前没有可领取的任务"})
   raise RuntimeError("Hub claim 响应缺少有效 claimed 布尔值")
  task=result.get("task")
  lease=result.get("lease")
  if not isinstance(task,dict) or not isinstance(lease,dict):
   raise RuntimeError("Hub claim 响应缺少任务或租约对象")
  try:
   task_id = _validated_task_id_value(task.get("id"))
  except RuntimeError:
   raise RuntimeError("Hub claim 响应缺少有效任务 ID") from None
  lease_token=lease.get("lease_token")
  lease_version=lease.get("lease_version")
  if (not isinstance(lease_token,str) or not 1 <= len(lease_token) <= 4096
      or any(ord(ch) < 32 or ord(ch) == 127 for ch in lease_token)
      or isinstance(lease_version,bool) or not isinstance(lease_version,int)
      or not 1 <= lease_version <= 2**31 - 1):
   raise RuntimeError("Hub claim 响应缺少有效租约字段")
  raw_expires_at = lease.get("lease_expires_at")
  if isinstance(raw_expires_at, bool) or not isinstance(raw_expires_at, (int, float)):
   raise RuntimeError("Hub claim 响应缺少有效租约过期时间")
  try:
   expires_at = float(raw_expires_at)
  except (OverflowError, TypeError, ValueError):
   raise RuntimeError("Hub claim 响应缺少有效租约过期时间") from None
  if not math.isfinite(expires_at) or expires_at <= time.time():
   raise RuntimeError("Hub 返回的任务租约已经过期或无效")
  history = task.get("history", [])
  if not isinstance(history, list) or any(not isinstance(message, dict) for message in history):
   raise RuntimeError("Hub claim 响应任务历史格式无效")
  if any(not isinstance(message.get("parts", []), list) for message in history):
   raise RuntimeError("Hub claim 响应任务内容格式无效")
  with LEASES_LOCK:
   if len(LEASES) >= MAX_ACTIVE_LEASES:
    raise RuntimeError("本地活动租约数量超过限制，拒绝覆盖现有租约")
   LEASES[task_id]={"lease_token":lease_token,"lease_version":lease_version,"lease_expires_at":expires_at}
 return _ok({"claimed":True,"task_id":task_id,"agent_id":task.get("agentId"),"state":(task.get("status") or {}).get("state"),"prompt":_task_prompt(task),"lease_expires_at":lease.get("lease_expires_at"),"note":"租约凭据由本地 MCP 进程保管，不会返回给模型。"})
def _validated_task_id_value(value: Any, field: str = "task_id") -> str:
 if not isinstance(value, str):
  raise RuntimeError(f"{field} 必须是字符串")
 normalized = value.strip()
 if (not normalized or len(normalized) > 128
     or any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized)):
  raise RuntimeError(f"{field} 无效")
 return normalized


def _validated_task_id(args: dict[str,Any]) -> str:
 return _validated_task_id_value(args.get("task_id"))


def tool_start(args: dict[str,Any]) -> dict[str,Any]:
 task_id = _validated_task_id(args)
 return _ok(_post(f"tasks/{urllib.parse.quote(task_id,safe='')}/start", _lease(task_id)))
def tool_renew(args: dict[str,Any]) -> dict[str,Any]:
 task_id = _validated_task_id(args)
 payload = _lease(task_id)
 result = _post(f"tasks/{urllib.parse.quote(task_id,safe='')}/renew", payload)
 raw_expires_at = result.get("lease_expires_at") if isinstance(result, dict) else None
 if isinstance(raw_expires_at, bool) or not isinstance(raw_expires_at, (int, float)):
  raise RuntimeError("Hub renew 响应缺少有效租约过期时间")
 try:
  expires_at = float(raw_expires_at)
 except (OverflowError, TypeError, ValueError):
  raise RuntimeError("Hub renew 响应缺少有效租约过期时间") from None
 if not math.isfinite(expires_at):
  raise RuntimeError("Hub renew 响应缺少有效租约过期时间")
 if expires_at <= time.time():
  raise RuntimeError("Hub renew 返回的租约已经过期")
 with LEASES_LOCK:
  current = LEASES.get(task_id)
  if (current is None or current.get("lease_token") != payload["lease_token"]
      or current.get("lease_version") != payload["lease_version"]):
   raise RuntimeError("Hub renew 回执对应的本地租约已变更；拒绝应用过期回执")
  current["lease_expires_at"] = expires_at
 return _ok(result)
def tool_progress(args: dict[str,Any]) -> dict[str,Any]:
 task_id = _validated_task_id(args)
 sequence = args.get("sequence")
 message = args.get("message")
 if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
  raise RuntimeError("sequence 必须是大于等于 1 的整数")
 if not isinstance(message, str) or not 1 <= len(message) <= 4000:
  raise RuntimeError("message 必须是 1 到 4000 字符的字符串")
 payload = {**_lease(task_id), "sequence": sequence, "message": message}
 return _ok(_post_idempotent(f"tasks/{urllib.parse.quote(task_id,safe='')}/progress", payload))
def tool_result(args: dict[str,Any]) -> dict[str,Any]:
 task_id = _validated_task_id(args)
 state = args.get("state")
 output = args.get("output")
 result_key = args.get("result_key")
 if state not in {"completed", "failed"}:
  raise RuntimeError("state 必须是 completed 或 failed")
 if not isinstance(output, str) or len(output) > 20000:
  raise RuntimeError("output 必须是最多 20000 字符的字符串")
 if result_key is None:
  # Hash the task identity so the deterministic default always fits the API
  # result_key limit, including maximum-length task IDs.
  result_key = (task_id + ":workbuddy-result-v1") if len(task_id) + len(":workbuddy-result-v1") <= 128 else ("wb-result-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest())
 elif not isinstance(result_key, str) or not 8 <= len(result_key) <= 128:
  raise RuntimeError("result_key 必须是 8 到 128 字符的字符串")
 payload = {**_lease(task_id), "result_key": result_key, "state": state, "output": output}
 result = _post_idempotent(f"tasks/{urllib.parse.quote(task_id,safe='')}/result", payload)
 with LEASES_LOCK:
  current = LEASES.get(task_id)
  if (current is None or current.get("lease_token") != payload["lease_token"]
      or current.get("lease_version") != payload["lease_version"]):
   raise RuntimeError("结果回执对应的本地租约已变更；拒绝清理新租约")
  LEASES.pop(task_id, None)
 return _ok(result)
TOOL_FUNCS={"workbuddy_connector_status":tool_status,"workbuddy_heartbeat":tool_heartbeat,"workbuddy_check_task_cancellation":tool_check_task_cancellation,"workbuddy_connection_check":tool_connection_check,"workbuddy_recovery_diagnostics":tool_recovery_diagnostics,"workbuddy_record_recovery_decision":tool_record_recovery_decision,"workbuddy_claim_task":tool_claim,"workbuddy_start_task":tool_start,"workbuddy_renew_task":tool_renew,"workbuddy_progress_task":tool_progress,"workbuddy_submit_result":tool_result}
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
  _write({"jsonrpc":"2.0","id":request_id,"result":{"protocolVersion":requested if requested in SUPPORTED else PROTOCOL_VERSION,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"a2a-hub-workbuddy-pull","version":"1.0.0"},"instructions":"连接故障时先调用 workbuddy_recovery_diagnostics 只读查看诊断；仅在用户明确选择后调用 workbuddy_record_recovery_decision 记录 acknowledged/deferred/dismissed。该回执不执行恢复动作。任务处理仍须由用户明确要求后才调用 workbuddy_claim_task；随后 start，长任务应在子步骤之间调用 workbuddy_check_task_cancellation 检查取消信号，should_stop=true 时立即停止且不再提交 progress/result；模型可用 progress/renew 更新状态，最后 submit_result。不要后台轮询。"}})
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
