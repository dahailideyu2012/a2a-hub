#!/usr/bin/env python3
"""Provision and operate a loopback-only disposable WorkBuddy Desktop sandbox."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_DIR = ROOT / "deploy" / "test"
ENV_FILE = SANDBOX_DIR / ".env.workbuddy-sandbox"
COMPOSE_FILE = SANDBOX_DIR / "workbuddy-sandbox.compose.yaml"
DEFAULT_PORT = 18081
SANDBOX_PROMPT = (
    "A2A-HUB-WORKBUDDY-SANDBOX-CANARY: reply with exactly "
    "WORKBUDDY_SANDBOX_OK and do not access files, networks, credentials, "
    "or external services."
)


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    return port


def create_env_file(path: Path, port: int = DEFAULT_PORT) -> None:
    validate_port(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "A2A_WORKBUDDY_SANDBOX_PORT": str(port),
        "A2A_API_TOKEN": secrets.token_urlsafe(48),
        "A2A_CHATGPT_INGRESS_TOKEN": secrets.token_urlsafe(48),
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": secrets.token_urlsafe(48),
    }
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid sandbox env file line {number}")
        key, value = line.split("=", 1)
        if not key or key in values or not value:
            raise ValueError(f"invalid or duplicate sandbox env key on line {number}")
        values[key] = value
    required = {
        "A2A_WORKBUDDY_SANDBOX_PORT", "A2A_API_TOKEN",
        "A2A_CHATGPT_INGRESS_TOKEN", "A2A_WORKBUDDY_CONNECTOR_TOKEN",
    }
    if not required.issubset(values):
        raise ValueError("sandbox env file is incomplete")
    validate_port(int(values["A2A_WORKBUDDY_SANDBOX_PORT"]))
    tokens = [values[key] for key in (
        "A2A_API_TOKEN", "A2A_CHATGPT_INGRESS_TOKEN",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN",
    )]
    if any(len(token) < 32 for token in tokens) or len(set(tokens)) != len(tokens):
        raise ValueError("invalid or reused sandbox credentials")
    return values


def _compose(*args: str) -> int:
    if not ENV_FILE.is_file():
        raise FileNotFoundError("sandbox env file missing; run init first")
    command = [
        "docker", "compose", "--env-file", str(ENV_FILE),
        "-f", str(COMPOSE_FILE), *args,
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def _request_json(method: str, path: str, port: int, token: str,
                  payload: dict[str, Any] | None = None,
                  idempotency_key: str | None = None) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"X-ChatGPT-Ingress-Token": token, "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("sandbox Hub response exceeded 1 MiB")
        if not 200 <= response.status < 300:
            raise RuntimeError(f"sandbox Hub returned HTTP {response.status}")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("sandbox Hub returned an invalid response")
        return result
    finally:
        connection.close()


def seed_task(path: Path = ENV_FILE) -> dict[str, str]:
    settings = load_env_file(path)
    result = _request_json(
        "POST", "/integrations/chatgpt/v1/tasks",
        int(settings["A2A_WORKBUDDY_SANDBOX_PORT"]),
        settings["A2A_CHATGPT_INGRESS_TOKEN"],
        {"agent_id": "workbuddy", "prompt": SANDBOX_PROMPT, "mode": "pull"},
        secrets.token_urlsafe(24),
    )
    task = result.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), str):
        raise RuntimeError("sandbox Hub response did not contain a task ID")
    status = task.get("status") or {}
    return {"task_id": task["id"], "state": str(status.get("state", "unknown"))}


def read_task(task_id: str, path: Path = ENV_FILE) -> dict[str, str]:
    if not task_id or len(task_id) > 128 or "/" in task_id or "\\" in task_id:
        raise ValueError("invalid task ID")
    settings = load_env_file(path)
    result = _request_json(
        "GET", f"/integrations/chatgpt/v1/tasks/{task_id}",
        int(settings["A2A_WORKBUDDY_SANDBOX_PORT"]),
        settings["A2A_CHATGPT_INGRESS_TOKEN"],
    )
    status = result.get("status") or {}
    return {
        "task_id": task_id,
        "state": str(status.get("state", "unknown")),
        "agent_id": str(result.get("agentId", "unknown")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("up")
    sub.add_parser("down")
    destroy = sub.add_parser("destroy")
    destroy.add_argument("--confirm-delete-data", action="store_true")
    sub.add_parser("seed")
    status = sub.add_parser("status")
    status.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            create_env_file(ENV_FILE, args.port)
            print(f"Created local sandbox credentials: {ENV_FILE.relative_to(ROOT)}")
            print(f"Loopback Hub URL: http://127.0.0.1:{args.port}")
            print("Secrets were not printed. Do not commit or paste the env file.")
            print("Next: python scripts/workbuddy_sandbox.py up")
            return 0
        if args.command == "up":
            return _compose("up", "-d", "--build")
        if args.command == "down":
            return _compose("down")
        if args.command == "destroy":
            if not args.confirm_delete_data:
                print("Refusing to delete sandbox data without --confirm-delete-data", file=sys.stderr)
                return 2
            return _compose("down", "--volumes")
        if args.command == "seed":
            print(json.dumps(seed_task(), ensure_ascii=False))
            return 0
        if args.command == "status":
            print(json.dumps(read_task(args.task_id), ensure_ascii=False))
            return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
