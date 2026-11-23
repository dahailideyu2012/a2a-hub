#!/usr/bin/env python3
"""Isolated real-Hub + real WorkBuddy desktop UI acceptance.

The coordinator starts a disposable Hub with temporary SQLite databases and a
loopback reverse proxy on 127.0.0.1:8080. WorkBuddy's existing stdio MCP bridge
connects to that proxy. The coordinator creates one synthetic task through the
ChatGPT REST ingress, waits for WorkBuddy AI to claim and complete it in the
desktop UI, then reads the result back through ChatGPT REST ingress.

The proxy checks only that the WorkBuddy token header name is present (it never
retrieves or inspects its value), then supplies a synthetic test token to the
isolated Hub. This does NOT validate the user's real token.
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = "127.0.0.1", 8080
CHATGPT_TOKEN = "test-chatgpt-ingress-token-only-000000000001"
WORKBUDDY_TOKEN = "test-workbuddy-connector-token-only-000000000002"
CONNECTOR_ID = "workbuddy-real-ui-test"
TASK_MARKER = "NETIVORA-A2A-REAL-HUB-UI-PASS"
EXPECTED_CALCULATION = "1517"
MAX_BODY = 32 * 1024
DEFAULT_TIMEOUT = 600


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(url: str, *, method: str = "GET", payload: dict | None = None,
              headers: dict[str, str] | None = None, expected: tuple[int, ...] = (200,)) -> tuple[int, dict]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("acceptance harness permits loopback HTTP only")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
        **(headers or {}),
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            status, raw = response.status, response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"{method} {parsed.path} failed: {type(exc).__name__}") from None
    if status not in expected:
        raise RuntimeError(f"{method} {parsed.path} returned HTTP {status}; expected {expected}")
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Hub response exceeded the 1 MiB acceptance limit")
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("Hub returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("Hub returned a non-object JSON body")
    return status, value

def copy_isolated_app(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "a2a_hub", destination / "a2a_hub")
    if (ROOT / "web").is_dir():
        shutil.copytree(ROOT / "web", destination / "web")
    return destination


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def task_text(task: dict[str, Any]) -> str:
    chunks: list[str] = []
    for message in task.get("history") or []:
        for part in message.get("parts") or []:
            value = part.get("text")
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks)


def proxy_action(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) >= 4 and parts[:3] == ["integrations", "workbuddy", "v1"]:
        return parts[3] if parts[3] == "claim" else (parts[5] if len(parts) > 5 else "unknown")
    return "unknown"


class ProxyState:
    def __init__(self, internal_url: str) -> None:
        self.internal_url = internal_url
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def record(self, action: str, status: int) -> None:
        with self.lock:
            self.events.append({"action": action, "http_status": status,
                                "at": datetime.now(timezone.utc).isoformat()})


class WorkBuddyProxyHandler(BaseHTTPRequestHandler):
    state: ProxyState

    def log_message(self, _format: str, *args: Any) -> None:
        # Never log headers, request bodies, or URLs with user-controlled data.
        return

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._reply(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _local(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def do_GET(self) -> None:
        if not self._local():
            self._json(403, {"detail": "loopback only"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/__test__/health":
            self._json(200, {"ok": True, "binding": f"{HOST}:{PORT}",
                             "upstream": "isolated loopback Hub"})
            return
        if path == "/__test__/report":
            with self.server.state.lock:
                events = list(self.server.state.events)
            self._json(200, {"suite": "workbuddy-real-hub-ui", "events": events})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self._local():
            self._json(403, {"detail": "loopback only"})
            return
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/integrations/workbuddy/v1/"
        if not path.startswith(prefix):
            self._json(404, {"detail": "only WorkBuddy connector routes are proxied"})
            return
        if "X-WorkBuddy-Connector-Token" not in self.headers:
            self._json(401, {"detail": "missing test credential header"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                self._json(413, {"detail": "request body too large"})
                return
            body = self.rfile.read(length)
            # Never forward the user's token. The isolated Hub receives a synthetic
            # test token, while the proxy only checks header presence/length.
            request = urllib.request.Request(
                self.server.state.internal_url + path,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN,
                },
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            try:
                with opener.open(request, timeout=10) as response:
                    status, raw = response.status, response.read(256 * 1024 + 1)
                    if len(raw) > 256 * 1024:
                        status, raw = 502, b'{"detail":"isolated Hub response exceeded the proxy limit"}'
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = json.dumps({"detail": f"isolated Hub returned HTTP {status}"}).encode("utf-8")
            action = proxy_action(path)
            self.server.state.record(action, status)
            self._reply(status, raw)
        except (OSError, ValueError, urllib.error.URLError):
            self._json(502, {"detail": "isolated Hub proxy request failed"})


class LoopbackProxy(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def stop_process(proc: subprocess.Popen | None) -> bool:
    if proc is None:
        return True
    if proc.poll() is None:
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    return proc.poll() is not None


def main() -> int:
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY",
                "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    )

    # Reserve the configured WorkBuddy endpoint first. If it is already used,
    # fail closed without stopping or modifying the existing listener.
    try:
        proxy = LoopbackProxy((HOST, PORT), WorkBuddyProxyHandler)
    except OSError as exc:
        print(json.dumps({
            "result": "BLOCKED",
            "reason": type(exc).__name__,
            "binding": f"{HOST}:{PORT}",
            "action": "Existing listener was not changed or stopped.",
        }), flush=True)
        return 2

    report: dict[str, Any] = {
        "suite": "workbuddy-real-hub-ui",
        "binding": f"{HOST}:{PORT}",
        "external_network": "not used",
        "production_access": False,
        "user_configuration_changed": False,
        "checks": {},
    }
    server: subprocess.Popen | None = None
    proxy_thread: threading.Thread | None = None
    exit_code = 1
    temp_context = tempfile.TemporaryDirectory(prefix="a2a-workbuddy-real-ui-")
    temp_root = Path(temp_context.name)
    try:
        isolated = copy_isolated_app(temp_root / "isolated-app")
        config_dir, data_dir = isolated / "config", isolated / "data"
        config_dir.mkdir(exist_ok=True)
        data_dir.mkdir(exist_ok=True)
        agents_path = config_dir / "agents.yaml"
        agents_path.write_text(
            "hub:\n  name: WorkBuddy Real UI Acceptance Hub\n"
            "agents:\n  - id: echo-a\n    name: Synthetic Acceptance Agent\n"
            "    type: echo\n    description: Synthetic no-side-effect test agent\n"
            "    enabled: true\n    priority: 1\n    tags: [test]\n"
            "    skills:\n      - id: acceptance\n        name: acceptance\n"
            "        description: isolated acceptance task\n        tags: [test]\n"
            "    config:\n      prefix: '[ui-acceptance]'\n",
            encoding="utf-8",
        )
        internal_port = free_loopback_port()
        internal_url = f"http://127.0.0.1:{internal_port}"
        db_path, ingress_db = data_dir / "tasks.sqlite3", data_dir / "ingress.sqlite3"
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("A2A_") or key.lower() in {
                "http_proxy", "https_proxy", "all_proxy", "pythonpath",
                "pythonhome", "pythonuserbase", "virtual_env",
            }:
                env.pop(key, None)
        env.update({
            "PYTHONPATH": str(isolated),
            "NO_PROXY": "*",
            "no_proxy": "*",
            "A2A_HOST": "127.0.0.1",
            "A2A_PORT": str(internal_port),
            "A2A_PUBLIC_URL": internal_url,
            "A2A_STORE": "sqlite",
            "A2A_DB_PATH": str(db_path),
            "A2A_AGENTS_FILE": str(agents_path),
            "A2A_API_TOKEN": "",
            "A2A_REQUIRE_AUTH": "false",
            "A2A_CHATGPT_INGRESS_TOKEN": CHATGPT_TOKEN,
            "A2A_CHATGPT_INGRESS_ACTOR": "human:workbuddy-real-ui-test",
            "A2A_CHATGPT_INGRESS_ALLOWED_AGENTS": "echo-a",
            "A2A_CHATGPT_INGRESS_DB_PATH": str(ingress_db),
            "A2A_CHATGPT_INGRESS_RATE_LIMIT": "30",
            "A2A_CHATGPT_INGRESS_MAX_BODY_BYTES": "16384",
            "A2A_WORKBUDDY_CONNECTOR_TOKEN": WORKBUDDY_TOKEN,
            "A2A_WORKBUDDY_CONNECTOR_ID": CONNECTOR_ID,
            "A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS": "echo-a",
            "A2A_WORKBUDDY_CONNECTOR_LEASE_SECONDS": "180",
            "A2A_MEMBERS_FILE": str(config_dir / "members-not-created.yaml"),
            "A2A_RELATIONS_FILE": str(data_dir / "relations.json"),
            "A2A_SOCIAL_MODE": "off",
            "A2A_AUTONOMY_ENABLED": "false",
            "A2A_LOG_LEVEL": "WARNING",
        })
        server_log = temp_root / "uvicorn.log"
        with server_log.open("w", encoding="utf-8") as log_file:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "a2a_hub.server:app",
                 "--host", "127.0.0.1", "--port", str(internal_port),
                 "--log-level", "warning"],
                cwd=str(isolated), env=env, stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("isolated Hub exited during startup")
            try:
                http_json(internal_url + "/health")
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("isolated Hub did not become healthy")

        proxy.state = ProxyState(internal_url)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        http_json(f"http://{HOST}:{PORT}/__test__/health")
        report["checks"]["isolated_hub_startup"] = "PASS"
        report["checks"]["loopback_proxy_health"] = "PASS"

        prompt = (
            "【隔离验收任务】这是 A2A Hub 与 WorkBuddy 桌面 AI 的真实 UI 联调。"
            "只处理本条合成任务，不访问互联网、不读取或修改本地文件、不调用其他工具、不查看凭据。"
            "请严格按顺序：1) 调用 workbuddy_start_task；"
            "2) 调用 workbuddy_progress_task，sequence=1，message 写明正在完成隔离计算；"
            "3) 调用 workbuddy_renew_task 一次；"
            "4) 计算 37 × 41，并将结果写入最终输出；"
            f"5) 调用 workbuddy_submit_result，state=completed，output 必须包含 {TASK_MARKER} 和计算结果 {EXPECTED_CALCULATION}。"
            "不要轮询领取第二项任务，不要把任何 token、环境变量或秘密写入进度/结果。"
        )
        _, submitted = http_json(
            internal_url + "/integrations/chatgpt/v1/tasks",
            method="POST",
            payload={"agent_id": "echo-a", "prompt": prompt, "mode": "pull"},
            headers={
                "X-ChatGPT-Ingress-Token": CHATGPT_TOKEN,
                "Idempotency-Key": "workbuddy-real-ui-task-0001",
            },
        )
        task = submitted.get("task") or {}
        task_id = str(task.get("id") or "")
        if not task_id:
            raise RuntimeError("ChatGPT ingress did not return a task ID")
        report["task_id"] = task_id
        report["checks"]["chatgpt_ingress_task_created"] = "PASS"
        print(json.dumps({
            "status": "READY_FOR_WORKBUDDY_UI",
            "task_id": task_id,
            "workbuddy_endpoint": f"http://{HOST}:{PORT}",
            "prompt_source": "isolated ChatGPT REST ingress",
            "instruction": (
                "现在切换到 WorkBuddy 桌面 AI 会话，要求它只领取并执行这一项 A2A Hub 任务；"
                "必须使用 MCP 工具，不得使用 PowerShell/curl/浏览器模拟调用。"
            ),
            "timeout_seconds": DEFAULT_TIMEOUT,
        }, ensure_ascii=False), flush=True)

        print("等待 WorkBuddy AI 在桌面 UI 中完成任务；按 Ctrl+C 可安全中止。", flush=True)
        deadline = time.monotonic() + DEFAULT_TIMEOUT
        final_task: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("isolated Hub exited while waiting for WorkBuddy")
            _, current = http_json(
                internal_url + f"/integrations/chatgpt/v1/tasks/{urllib.parse.quote(task_id, safe='')}",
                headers={"X-ChatGPT-Ingress-Token": CHATGPT_TOKEN},
            )
            state = (current.get("status") or {}).get("state")
            if state in {"completed", "failed", "canceled", "rejected"}:
                final_task = current
                break
            time.sleep(1.0)
        if final_task is None:
            report["checks"]["workbuddy_ui_task_completion"] = "TIMEOUT"
            raise TimeoutError("WorkBuddy UI task did not reach a terminal state within the acceptance window")

        text = task_text(final_task)
        status = (final_task.get("status") or {}).get("state")
        report["task_state"] = status
        report["checks"]["workbuddy_ui_task_completion"] = (
            "PASS" if status == "completed" and TASK_MARKER in text and EXPECTED_CALCULATION in text
            else "FAIL"
        )
        with proxy.state.lock:
            events = list(proxy.state.events)
        report["workbuddy_tool_events"] = events
        expected_actions = ["claim", "start", "progress", "renew", "result"]
        actual_actions = [event["action"] for event in events]
        report["checks"]["workbuddy_api_sequence"] = (
            "PASS" if actual_actions == expected_actions and all(e["http_status"] == 200 for e in events)
            else "FAIL"
        )
        report["result"] = (
            "PASS" if report["checks"]["workbuddy_ui_task_completion"] == "PASS"
            and report["checks"]["workbuddy_api_sequence"] == "PASS" else "FAIL"
        )
        report["chatgpt_readback"] = {
            "task_state": status,
            "marker_present": TASK_MARKER in text,
            "calculation_present": EXPECTED_CALCULATION in text,
        }
        exit_code = 0 if report["result"] == "PASS" else 1
    except KeyboardInterrupt:
        report["result"] = "ABORTED"
        exit_code = 130
    except Exception as exc:
        report["result"] = "FAIL"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        exit_code = 1
    finally:
        if proxy_thread is not None and proxy_thread.is_alive():
            try:
                proxy.shutdown()
            except Exception:
                pass
        proxy.server_close()
        if proxy_thread is not None:
            proxy_thread.join(timeout=3)
        proxy_state = getattr(proxy, "state", None)
        if proxy_state is not None:
            with proxy_state.lock:
                report.setdefault("workbuddy_tool_events", list(proxy_state.events))
        hub_stopped = stop_process(server)
        report["hub_process_exited"] = hub_stopped
        try:
            temp_context.cleanup()
            report["temporary_directory_cleanup"] = "PASS"
        except OSError as exc:
            report["temporary_directory_cleanup"] = f"FAIL: {type(exc).__name__}"
            report["result"] = "FAIL"
            exit_code = 1
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
