#!/usr/bin/env python3
"""Disposable loopback-only fake Hub for real WorkBuddy desktop UI acceptance.

Binds only 127.0.0.1:8080; serves one synthetic task; makes no outbound
requests; writes no files; and never logs or returns the supplied token.
The fixture checks only that a >=32-character token header is present, not
its value. Real token authentication is covered by the Hub integration tests.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

HOST, PORT = "127.0.0.1", 8080
TASK_ID = "wb-ui-fixture-0001"
TOKEN_HEADER = "X-WorkBuddy-Connector-Token"
PROMPT = (
    "这是一次隔离的 WorkBuddy 桌面 UI 工具调用验收。只处理本测试任务，不访问网络、"
    "不读取或修改文件、不查看任何凭据。请按顺序调用 workbuddy_start_task，"
    "workbuddy_progress_task（sequence=1，message 说明已开始本地 UI 验收），"
    "workbuddy_renew_task（续租一次），最后调用 workbuddy_submit_result，"
    "state=completed，output 必须包含 NETIVORA-WORKBUDDY-UI-ROUNDTRIP-PASS。"
    "不要把 token 或环境变量放进 progress 或 result。"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.lease_token: str | None = None
        self.lease_version = 0
        self.lease_expires = 0.0
        self.claimed = False
        self.started = False
        self.progress_sequence = 0
        self.result: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.task: dict[str, Any] = {
            "id": TASK_ID,
            "agentId": "workbuddy-ui-fixture-agent",
            "status": {"state": "submitted"},
            "history": [{"role": "user", "parts": [{"kind": "text", "text": PROMPT}]}],
            "createdAt": now(),
            "updatedAt": now(),
        }

    def record(self, event: str, **extra: Any) -> None:
        self.events.append({"event": event, "at": now(), **extra})

    def report(self) -> dict[str, Any]:
        with self.lock:
            renewed = any(item["event"] == "renew" for item in self.events)
            marker = "NETIVORA-WORKBUDDY-UI-ROUNDTRIP-PASS"
            passed = bool(
                self.claimed and self.started and self.progress_sequence >= 1
                and renewed and self.result
                and self.result.get("state") == "completed"
                and marker in str(self.result.get("output", ""))
            )
            return {
                "suite": "workbuddy-real-ui-loopback-fixture",
                "binding": f"{HOST}:{PORT}",
                "external_network": "not used",
                "task_id": TASK_ID,
                "claimed": self.claimed,
                "started": self.started,
                "progress_sequence": self.progress_sequence,
                "renewed": renewed,
                "final_state": self.result.get("state") if self.result else None,
                "final_output": self.result.get("output") if self.result else None,
                "events": list(self.events),
                "result": "PASS" if passed else "NOT_COMPLETE",
            }


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "A2A-WorkBuddy-UI-Fixture/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        # Never log request headers, query strings, or user-supplied content.
        return

    def send_json(self, code: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_only(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def authorized_header_present(self) -> bool:
        return len(self.headers.get(TOKEN_HEADER, "")) >= 32

    def read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16384:
                return None
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:
        if not self.local_only():
            self.send_json(403, {"detail": "loopback only"})
        elif urlsplit(self.path).path == "/__test__/health":
            self.send_json(200, {"ok": True, "binding": f"{HOST}:{PORT}"})
        elif urlsplit(self.path).path == "/__test__/report":
            self.send_json(200, STATE.report())
        else:
            self.send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self.local_only():
            self.send_json(403, {"detail": "loopback only"})
            return
        path = urlsplit(self.path).path
        if path == "/__test__/shutdown":
            self.send_json(200, {"shutdown": "requested"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if not self.authorized_header_present():
            self.send_json(401, {"detail": "missing or invalid test header"})
            return
        body = self.read_json()
        if body is None:
            self.send_json(400, {"detail": "invalid JSON object"})
            return

        with STATE.lock:
            if path == "/integrations/workbuddy/v1/claim":
                if STATE.claimed or STATE.result:
                    self.send_json(200, {"claimed": False})
                    return
                requested = body.get("agent_ids") or []
                if requested and "workbuddy-ui-fixture-agent" not in requested:
                    self.send_json(200, {"claimed": False})
                    return
                STATE.claimed = True
                STATE.lease_version += 1
                STATE.lease_token = secrets.token_urlsafe(32)
                STATE.lease_expires = time.time() + 300
                STATE.record("claim")
                self.send_json(200, {
                    "claimed": True,
                    "task": STATE.task,
                    "lease": {
                        "connector_id": "workbuddy-ui-fixture",
                        "lease_token": STATE.lease_token,
                        "lease_version": STATE.lease_version,
                        "lease_expires_at": STATE.lease_expires,
                    },
                })
                return

            prefix = "/integrations/workbuddy/v1/tasks/"
            if not path.startswith(prefix):
                self.send_json(404, {"detail": "not found"})
                return
            parts = path[len(prefix):].split("/")
            if len(parts) != 2 or parts[0] != TASK_ID or parts[1] not in {
                "start", "renew", "progress", "result"
            }:
                self.send_json(404, {"detail": "not found"})
                return
            if not STATE.claimed or not STATE.lease_token:
                self.send_json(409, {"detail": "task not claimed"})
                return
            if (
                body.get("lease_token") != STATE.lease_token
                or body.get("lease_version") != STATE.lease_version
                or time.time() >= STATE.lease_expires
            ):
                self.send_json(409, {"detail": "lease conflict"})
                return

            action = parts[1]
            if action == "start":
                if STATE.started:
                    self.send_json(409, {"detail": "already started"})
                    return
                STATE.started = True
                STATE.task["status"] = {"state": "working"}
                STATE.task["updatedAt"] = now()
                STATE.record("start")
                self.send_json(200, STATE.task)
            elif action == "renew":
                STATE.lease_expires = time.time() + 300
                STATE.record("renew")
                self.send_json(200, {"lease_expires_at": STATE.lease_expires})
            elif action == "progress":
                seq, message = body.get("sequence"), body.get("message")
                if not isinstance(seq, int) or seq <= STATE.progress_sequence:
                    self.send_json(409, {"detail": "progress sequence conflict"})
                    return
                if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                    self.send_json(400, {"detail": "invalid progress message"})
                    return
                STATE.progress_sequence = seq
                STATE.task["history"].append({
                    "role": "agent", "parts": [{"kind": "text", "text": message}]
                })
                STATE.task["updatedAt"] = now()
                STATE.record("progress", sequence=seq)
                self.send_json(200, {"task": STATE.task, "idempotent_replay": False})
            else:
                result_state, output = body.get("state"), body.get("output", "")
                if result_state not in {"completed", "failed"} or not isinstance(output, str):
                    self.send_json(400, {"detail": "invalid result"})
                    return
                STATE.result = {"state": result_state, "output": output}
                STATE.task["status"] = {"state": result_state}
                STATE.task["history"].append({
                    "role": "agent", "parts": [{"kind": "text", "text": output}]
                })
                STATE.task["updatedAt"] = now()
                STATE.record("result", state=result_state)
                self.send_json(200, {"task": STATE.task, "idempotent_replay": False})


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def main() -> int:
    try:
        server = LoopbackServer((HOST, PORT), Handler)
    except OSError as exc:
        print(json.dumps({
            "result": "BLOCKED",
            "reason": type(exc).__name__,
            "binding": f"{HOST}:{PORT}",
            "action": "Existing listener was not changed or stopped.",
        }), flush=True)
        return 2

    print(json.dumps({
        "status": "READY",
        "binding": f"http://{HOST}:{PORT}",
        "task_id": TASK_ID,
        "health": f"http://{HOST}:{PORT}/__test__/health",
        "report": f"http://{HOST}:{PORT}/__test__/report",
        "note": "Loopback-only disposable fixture; press Ctrl+C to stop.",
    }), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(json.dumps(STATE.report(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
