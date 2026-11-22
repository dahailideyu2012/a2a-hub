#!/usr/bin/env python3
"""Isolated, process-level ChatGPT <-> WorkBuddy loopback acceptance.

Run from a checkout with project dependencies installed:
    python scripts/workbuddy_loopback_acceptance.py

The runner copies only application source into a fresh temporary root (therefore
no repository .env/config/database is loaded), starts Uvicorn on 127.0.0.1,
and drives the real WorkBuddy MCP stdio process. It never contacts public hosts.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHATGPT_TOKEN = "test-chatgpt-ingress-token-only-000000000001"
WORKBUDDY_TOKEN = "test-workbuddy-connector-token-only-000000000002"
CONNECTOR_ID = "workbuddy-loopback-test"
TIMEOUT = 12.0


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, *, method: str = "GET", payload: dict | None = None,
              headers: dict[str, str] | None = None, expected: tuple[int, ...] = (200,)) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, raw = response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(1024 * 1024)
    if status not in expected:
        raise AssertionError(f"{method} {url} returned HTTP {status}; expected {expected}: {raw[:500]!r}")
    value = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(value, dict):
        raise AssertionError(f"{method} {url} returned a non-object JSON body")
    return status, value


class McpStdio:
    def __init__(self, script: Path, env: dict[str, str], cwd: Path, stderr_path: Path):
        self.stderr_file = stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr_file, text=True, encoding="utf-8", bufsize=1,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.next_id = 0

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def request(self, method: str, params: dict | None = None) -> dict:
        if self.proc.poll() is not None:
            raise AssertionError(f"MCP process exited early with code {self.proc.returncode}")
        self.next_id += 1
        request_id = self.next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for MCP {method} response")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(f"Timed out waiting for MCP {method} response") from None
            if line is None:
                raise AssertionError(f"MCP stdout closed while waiting for {method}")
            response = json.loads(line)
            if response.get("id") == request_id:
                if "error" in response:
                    raise AssertionError(f"MCP protocol error for {method}: {response['error']}")
                return response.get("result") or {}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            message = (result.get("content") or [{}])[0].get("text", "unknown MCP tool error")
            raise AssertionError(f"MCP tool {name} failed: {message}")
        text = (result.get("content") or [{}])[0].get("text", "{}")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise AssertionError(f"MCP tool {name} returned non-object JSON")
        return value

    def close(self) -> bool:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        if self.stderr_file and not self.stderr_file.closed:
            self.stderr_file.close()
        return self.proc.poll() is not None


def copy_isolated_app(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "a2a_hub", destination / "a2a_hub")
    if (ROOT / "web").is_dir():
        shutil.copytree(ROOT / "web", destination / "web")
    shutil.copy2(ROOT / "workbuddy_mcp_server.py", destination / "workbuddy_mcp_server.py")
    # Deliberately do not copy .env, data/, config/, .git/, tests/, or .audit-tmp/.
    return destination


def task_prompt(round_no: int, dependency: str = "") -> str:
    if round_no == 1:
        return "Loopback acceptance round 1: report the exact phrase ROUND_ONE_OK."
    return f"Loopback acceptance round 2, derived from round 1 result [{dependency}]: report ROUND_TWO_OK."


def main() -> int:
    # Force direct local HTTP for the parent process; desktop/system proxy
    # settings must never forward loopback acceptance traffic externally.
    for proxy_key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(proxy_key, None)
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )

    report: dict[str, Any] = {
        "suite": "workbuddy-loopback-process-acceptance",
        "binding": "127.0.0.1 only",
        "external_network": "not used",
        "checks": {},
    }
    server: subprocess.Popen | None = None
    mcp: McpStdio | None = None
    temp_context = tempfile.TemporaryDirectory(prefix="a2a-workbuddy-loopback-")
    temp_root = Path(temp_context.name)
    try:
        isolated = copy_isolated_app(temp_root / "isolated-app")
        config_dir = isolated / "config"
        data_dir = isolated / "data"
        config_dir.mkdir(exist_ok=True)
        data_dir.mkdir(exist_ok=True)
        agents_path = config_dir / "agents.yaml"
        agents_path.write_text(
            "hub:\n  name: Loopback Acceptance Hub\n"
            "agents:\n  - id: echo-a\n    name: Loopback Echo\n"
            "    type: echo\n    description: Isolated process acceptance agent\n"
            "    enabled: true\n    priority: 1\n    tags: [test]\n"
            "    skills:\n      - id: loopback\n        name: loopback\n"
            "        description: isolated loopback test\n        tags: [test]\n"
            "    config:\n      prefix: '[loopback]'\n",
            encoding="utf-8",
        )
        port = free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        db_path = data_dir / "tasks.sqlite3"
        ingress_db = data_dir / "ingress.sqlite3"
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
            "A2A_PORT": str(port),
            "A2A_PUBLIC_URL": base_url,
            "A2A_STORE": "sqlite",
            "A2A_DB_PATH": str(db_path),
            "A2A_AGENTS_FILE": str(agents_path),
            "A2A_API_TOKEN": "",
            "A2A_REQUIRE_AUTH": "false",
            "A2A_CHATGPT_INGRESS_TOKEN": CHATGPT_TOKEN,
            "A2A_CHATGPT_INGRESS_ACTOR": "human:loopback-test",
            "A2A_CHATGPT_INGRESS_ALLOWED_AGENTS": "echo-a",
            "A2A_CHATGPT_INGRESS_DB_PATH": str(ingress_db),
            "A2A_CHATGPT_INGRESS_RATE_LIMIT": "120",
            "A2A_CHATGPT_INGRESS_MAX_BODY_BYTES": "16384",
            "A2A_WORKBUDDY_CONNECTOR_TOKEN": WORKBUDDY_TOKEN,
            "A2A_WORKBUDDY_CONNECTOR_ID": CONNECTOR_ID,
            "A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS": "echo-a",
            "A2A_WORKBUDDY_CONNECTOR_LEASE_SECONDS": "120",
            "A2A_MEMBERS_FILE": str(config_dir / "members-do-not-create.yaml"),
            "A2A_RELATIONS_FILE": str(data_dir / "relations.json"),
            "A2A_SOCIAL_MODE": "off",
            "A2A_AUTONOMY_ENABLED": "false",
            "A2A_LOG_LEVEL": "WARNING",
        })
        server_log = temp_root / "uvicorn.log"
        with server_log.open("w", encoding="utf-8") as log_file:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "a2a_hub.server:app",
                 "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=str(isolated), env=env, stdout=log_file, stderr=subprocess.STDOUT,
                # A separate process group lets the Windows runner send CTRL_BREAK
                # for a graceful Uvicorn shutdown (FastAPI lifespan must close SQLite).
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise AssertionError(f"Isolated Hub exited during startup: {server_log.read_text(encoding='utf-8')[-2000:]}")
                try:
                    http_json(base_url + "/health")
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise AssertionError("Isolated Hub did not become healthy on loopback")
        report["checks"]["isolated_hub_startup"] = "PASS"
        report["checks"]["server_bind"] = "127.0.0.1"

        # Authentication is tested before any successful task traffic.
        http_json(base_url + "/integrations/chatgpt/v1/tasks", method="POST",
                  payload={"agent_id": "echo-a", "prompt": "bad auth", "mode": "pull"},
                  headers={"X-ChatGPT-Ingress-Token": "wrong-ingress-token-value-000000"},
                  expected=(403,))
        http_json(base_url + "/integrations/workbuddy/v1/claim", method="POST", payload={},
                  headers={"X-WorkBuddy-Connector-Token": "wrong-connector-token-value-000000"},
                  expected=(403,))
        report["checks"]["wrong_chatgpt_token"] = "PASS (HTTP 403)"
        report["checks"]["wrong_workbuddy_token"] = "PASS (HTTP 403)"

        # Real MCP stdio process: initialize + tools/list, then lifecycle calls.
        mcp_env = env.copy()
        mcp_env["A2A_WORKBUDDY_HUB_URL"] = base_url
        mcp_env["A2A_WORKBUDDY_CONNECTOR_TOKEN"] = WORKBUDDY_TOKEN
        mcp = McpStdio(isolated / "workbuddy_mcp_server.py", mcp_env, isolated, temp_root / "mcp.stderr.log")
        initialized = mcp.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "loopback-test", "version": "1.0"}})
        tools = mcp.request("tools/list").get("tools", [])
        expected_tools = {
            "workbuddy_claim_task", "workbuddy_start_task", "workbuddy_renew_task",
            "workbuddy_progress_task", "workbuddy_submit_result",
        }
        actual_tools = {item.get("name") for item in tools}
        if actual_tools != expected_tools or len(tools) != 5:
            raise AssertionError(f"MCP tools/list mismatch: {sorted(actual_tools)}")
        report["checks"]["mcp_initialize"] = "PASS"
        report["checks"]["mcp_tools_list"] = "PASS (exactly five expected tools)"

        ingress_headers = {"X-ChatGPT-Ingress-Token": CHATGPT_TOKEN}
        first_result = ""
        for round_no in (1, 2):
            prompt = task_prompt(round_no, first_result)
            _, submitted = http_json(
                base_url + "/integrations/chatgpt/v1/tasks", method="POST",
                headers={**ingress_headers, "Idempotency-Key": f"loopback-round-{round_no:02d}-0001"},
                payload={"agent_id": "echo-a", "prompt": prompt, "mode": "pull"},
            )
            task_id = submitted["task"]["id"]
            if submitted.get("idempotent_replay"):
                raise AssertionError("Fresh round unexpectedly replayed an old idempotency key")
            claim = mcp.call_tool("workbuddy_claim_task")
            if not claim.get("claimed") or claim.get("task_id") != task_id:
                raise AssertionError(f"WorkBuddy MCP did not claim round {round_no} task")
            mcp.call_tool("workbuddy_start_task", {"task_id": task_id})
            mcp.call_tool("workbuddy_progress_task", {
                "task_id": task_id, "sequence": 1, "message": f"round {round_no}: running process-level MCP test",
            })
            expected_phrase = "ROUND_ONE_OK" if round_no == 1 else "ROUND_TWO_OK"
            output = f"{expected_phrase}; round={round_no}; derived_from={first_result or 'initial'}"
            mcp.call_tool("workbuddy_submit_result", {
                "task_id": task_id, "state": "completed", "output": output,
            })
            _, readback = http_json(
                base_url + f"/integrations/chatgpt/v1/tasks/{task_id}",
                headers=ingress_headers,
            )
            if readback.get("status", {}).get("state") != "completed" or expected_phrase not in json.dumps(readback):
                raise AssertionError(f"ChatGPT readback failed for round {round_no}")
            if round_no == 1:
                first_result = expected_phrase
            report["checks"][f"round_{round_no}_chatgpt_to_workbuddy_to_chatgpt"] = "PASS"

        # Duplicate result idempotency is verified against the real HTTP endpoint.
        _, third = http_json(
            base_url + "/integrations/chatgpt/v1/tasks", method="POST",
            headers={**ingress_headers, "Idempotency-Key": "loopback-idempotency-0001"},
            payload={"agent_id": "echo-a", "prompt": "idempotency case", "mode": "pull"},
        )
        third_id = third["task"]["id"]
        _, direct_claim = http_json(base_url + "/integrations/workbuddy/v1/claim", method="POST",
                                    headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload={})
        lease = direct_claim["lease"]
        lease_body = {"lease_token": lease["lease_token"], "lease_version": lease["lease_version"]}
        http_json(base_url + f"/integrations/workbuddy/v1/tasks/{third_id}/start", method="POST",
                  headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload=lease_body)
        result_body = {**lease_body, "result_key": "loopback-result-idempotent-01",
                       "state": "completed", "output": "idempotency result"}
        http_json(base_url + f"/integrations/workbuddy/v1/tasks/{third_id}/result", method="POST",
                  headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload=result_body)
        _, replay = http_json(base_url + f"/integrations/workbuddy/v1/tasks/{third_id}/result", method="POST",
                              headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload=result_body)
        if replay.get("idempotent_replay") is not True:
            raise AssertionError("Duplicate result was not recognized as an idempotent replay")
        report["checks"]["duplicate_result_idempotency"] = "PASS"

        # Lease fencing: expire an old lease in the isolated DB, reclaim, reject stale progress.
        _, fourth = http_json(
            base_url + "/integrations/chatgpt/v1/tasks", method="POST",
            headers={**ingress_headers, "Idempotency-Key": "loopback-fencing-0001"},
            payload={"agent_id": "echo-a", "prompt": "lease fencing case", "mode": "pull"},
        )
        fourth_id = fourth["task"]["id"]
        _, old_claim = http_json(base_url + "/integrations/workbuddy/v1/claim", method="POST",
                                 headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload={})
        old_lease = old_claim["lease"]
        # sqlite3.Connection.__exit__ commits/rolls back but does NOT close
        # the connection. Explicitly close it before TemporaryDirectory cleanup,
        # otherwise Windows keeps tasks.sqlite3 locked until this function returns.
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, fourth_id))
        _, new_claim = http_json(base_url + "/integrations/workbuddy/v1/claim", method="POST",
                                 headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload={})
        new_lease = new_claim["lease"]
        if new_lease["lease_version"] != old_lease["lease_version"] + 1:
            raise AssertionError("Lease version did not advance after expiry")
        http_json(base_url + f"/integrations/workbuddy/v1/tasks/{fourth_id}/progress", method="POST",
                  headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN},
                  payload={"lease_token": old_lease["lease_token"], "lease_version": old_lease["lease_version"],
                           "sequence": 1, "message": "stale lease"},
                  expected=(409,))
        report["checks"]["lease_fencing"] = "PASS (stale lease rejected with HTTP 409)"

        # ChatGPT cancellation must fence the active WorkBuddy lease.
        _, fifth = http_json(
            base_url + "/integrations/chatgpt/v1/tasks", method="POST",
            headers={**ingress_headers, "Idempotency-Key": "loopback-cancel-0001"},
            payload={"agent_id": "echo-a", "prompt": "cancel fencing case", "mode": "pull"},
        )
        fifth_id = fifth["task"]["id"]
        _, cancel_claim = http_json(base_url + "/integrations/workbuddy/v1/claim", method="POST",
                                    headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN}, payload={})
        cancel_lease = cancel_claim["lease"]
        http_json(base_url + f"/integrations/chatgpt/v1/tasks/{fifth_id}/cancel", method="POST",
                  headers=ingress_headers)
        http_json(base_url + f"/integrations/workbuddy/v1/tasks/{fifth_id}/progress", method="POST",
                  headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN},
                  payload={"lease_token": cancel_lease["lease_token"], "lease_version": cancel_lease["lease_version"],
                           "sequence": 1, "message": "must be fenced"},
                  expected=(409,))
        http_json(base_url + f"/integrations/workbuddy/v1/tasks/{fifth_id}/result", method="POST",
                  headers={"X-WorkBuddy-Connector-Token": WORKBUDDY_TOKEN},
                  payload={"lease_token": cancel_lease["lease_token"], "lease_version": cancel_lease["lease_version"],
                           "result_key": "cancelled-result-key-01",
                           "state": "completed", "output": "must be rejected"},
                  expected=(409,))
        report["checks"]["cancel_fences_old_lease"] = "PASS (progress/result rejected with HTTP 409)"

        report["checks"]["task_database_isolated"] = db_path.is_file() and ingress_db.is_file()
        report["checks"]["production_config_copied"] = False
        report["checks"]["production_database_used"] = False
        report["checks"]["real_credentials_used"] = False
        report["checks"]["public_network_used"] = False
        report["task_rounds_completed"] = 2
        report["result"] = "PASS"
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Cleanup must never suppress the JSON report. Windows can briefly keep
        # SQLite files locked after the Uvicorn process exits, so retry removal.
        if mcp is not None:
            try:
                mcp_closed = mcp.close()
                report["checks"]["mcp_process_cleanup"] = "PASS" if mcp_closed else "FAIL"
            except Exception as exc:
                report["checks"]["mcp_process_cleanup"] = f"FAIL: {type(exc).__name__}: {exc}"
        else:
            report["checks"]["mcp_process_cleanup"] = "PASS (not started)"

        if server is not None:
            try:
                if server.poll() is None:
                    # Ask Uvicorn to perform its lifespan shutdown first. On Windows,
                    # terminate() is a hard kill and skips lifespan cleanup, leaving
                    # SQLite handles open and making TemporaryDirectory deletion fail.
                    try:
                        if os.name == "nt":
                            server.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            server.send_signal(signal.SIGINT)
                        server.wait(timeout=12)
                    except (OSError, subprocess.TimeoutExpired):
                        if server.poll() is None:
                            server.terminate()
                            try:
                                server.wait(timeout=8)
                            except subprocess.TimeoutExpired:
                                server.kill()
                                server.wait(timeout=5)
                report["checks"]["hub_process_cleanup"] = (
                    "PASS" if server.poll() is not None else "FAIL (process still running)"
                )
            except Exception as exc:
                report["checks"]["hub_process_cleanup"] = f"FAIL: {type(exc).__name__}: {exc}"
        else:
            report["checks"]["hub_process_cleanup"] = "PASS (not started)"

        cleanup_error = None
        for attempt in range(6):
            try:
                temp_context.cleanup()
                cleanup_error = None
                break
            except OSError as exc:
                cleanup_error = exc
                # Give Windows file handles a chance to close before retrying.
                time.sleep(0.25 * (attempt + 1))
        if cleanup_error is None:
            report["temporary_directory_cleanup"] = "PASS (isolated temporary root removed)"
        else:
            report["temporary_directory_cleanup"] = (
                f"FAIL: {type(cleanup_error).__name__}: {cleanup_error}"
            )
            report["result"] = "FAIL"
            report["error"] = report.get("error") or "Temporary directory cleanup failed"

        cleanup_failed = any(
            str(value).startswith("FAIL") for value in report["checks"].values()
        ) or str(report.get("temporary_directory_cleanup", "")).startswith("FAIL")
        if cleanup_failed:
            report["result"] = "FAIL"
    # Always emit the structured report, including cleanup failures.
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("result") == "PASS" and not cleanup_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
