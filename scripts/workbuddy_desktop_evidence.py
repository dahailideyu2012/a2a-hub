#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline structural validator for operator-attested WorkBuddy Desktop evidence.

This tool never connects to the Hub, reads credentials, launches WorkBuddy, or
claims cryptographic authenticity. A PASS means only that the supplied
operator-attested evidence manifest is structurally consistent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = "1.0"
REQUIRED_EVENTS = (
    "connector_imported",
    "mcp_server_started",
    "handshake_accepted",
    "presence_online",
    "heartbeat_accepted",
)
ALLOWED_SOURCES = {"desktop_ui", "hub_receipt", "operator_observation"}
SECRET_KEY_PARTS = ("token", "secret", "password", "authorization", "cookie", "private_key", "api_key")


def _is_nonempty(value: Any, limit: int = 200) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and value == value.strip()


def _timestamp(value: Any) -> bool:
    if not _is_nonempty(value, 64):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _safe_hub_origin(value: Any, allow_loopback_http: bool) -> bool:
    if not _is_nonempty(value, 512):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.path not in ("", "/") or not parsed.hostname:
            return False
        if parsed.scheme == "https":
            return True
        return (
            allow_loopback_http
            and parsed.scheme == "http"
            and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        )
    except ValueError:
        return False


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in SECRET_KEY_PARTS):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def validate_evidence(data: Any, *, allow_loopback_http: bool = False) -> list[str]:
    """Return validation errors; never echo user-provided values."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["根节点必须是 JSON object"]
    if _contains_secret_key(data):
        errors.append("证据文件包含疑似凭据字段；请删除 Token、密码、Cookie、授权头或私钥")

    allowed_top = {"schema_version", "evidence_type", "client", "connector", "hub", "session", "events", "attestation"}
    if set(data) - allowed_top:
        errors.append("根节点包含不支持的字段")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 必须为 1.0")
    if data.get("evidence_type") != "workbuddy_desktop_connection":
        errors.append("evidence_type 不正确")

    client = data.get("client")
    if not isinstance(client, dict) or set(client) != {"product", "version"}:
        errors.append("client 必须且仅包含 product、version")
    elif client.get("product") != "WorkBuddy Desktop" or not _is_nonempty(client.get("version"), 80):
        errors.append("client 产品名称或版本无效")

    connector = data.get("connector")
    if not isinstance(connector, dict) or set(connector) != {"id"}:
        errors.append("connector 必须且仅包含 id")
    elif not _is_nonempty(connector.get("id"), 64) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", connector["id"]):
        errors.append("connector id 格式无效")

    hub = data.get("hub")
    if not isinstance(hub, dict) or set(hub) != {"origin", "environment"}:
        errors.append("hub 必须且仅包含 origin、environment")
    else:
        if not _safe_hub_origin(hub.get("origin"), allow_loopback_http):
            errors.append("Hub origin 必须是无凭据、无路径参数的 HTTPS 根地址（本地测试可显式允许 loopback HTTP）")
        if hub.get("environment") not in {"production", "staging", "local"}:
            errors.append("hub environment 必须是 production、staging 或 local")

    session = data.get("session")
    if not isinstance(session, dict) or set(session) != {"id", "started_at", "completed_at"}:
        errors.append("session 必须且仅包含 id、started_at、completed_at")
    else:
        if not _is_nonempty(session.get("id"), 128):
            errors.append("session id 无效")
        if not _timestamp(session.get("started_at")) or not _timestamp(session.get("completed_at")):
            errors.append("session 时间必须是带时区的 ISO-8601 时间")
        elif datetime.fromisoformat(session["completed_at"].replace("Z", "+00:00")) < datetime.fromisoformat(session["started_at"].replace("Z", "+00:00")):
            errors.append("session completed_at 不能早于 started_at")

    events = data.get("events")
    if not isinstance(events, list) or len(events) != len(REQUIRED_EVENTS):
        errors.append("events 必须按顺序包含全部 5 个连接验收事件")
    else:
        for index, (event, expected_name) in enumerate(zip(events, REQUIRED_EVENTS)):
            if not isinstance(event, dict) or set(event) != {"name", "status", "observed_at", "source", "evidence_ref"}:
                errors.append(f"events[{index}] 字段结构无效")
                continue
            if event.get("name") != expected_name:
                errors.append(f"events[{index}] 顺序或事件名称不正确")
            if event.get("status") != "passed":
                errors.append(f"events[{index}] 未标记为 passed")
            if not _timestamp(event.get("observed_at")):
                errors.append(f"events[{index}] observed_at 必须是带时区的 ISO-8601 时间")
            if event.get("source") not in ALLOWED_SOURCES:
                errors.append(f"events[{index}] source 不受支持")
            if not _is_nonempty(event.get("evidence_ref"), 160):
                errors.append(f"events[{index}] evidence_ref 无效")

    # Event timestamps must form a chronological timeline inside the declared session.
    if (
        isinstance(session, dict)
        and _timestamp(session.get("started_at"))
        and _timestamp(session.get("completed_at"))
        and isinstance(events, list)
        and len(events) == len(REQUIRED_EVENTS)
    ):
        session_start = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00"))
        session_end = datetime.fromisoformat(session["completed_at"].replace("Z", "+00:00"))
        observed_times = []
        for index, event in enumerate(events):
            if isinstance(event, dict) and _timestamp(event.get("observed_at")):
                observed = datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00"))
                observed_times.append(observed)
                if observed < session_start or observed > session_end:
                    errors.append(f"events[{index}] observed_at 必须位于 session 时间范围内")
        if len(observed_times) == len(REQUIRED_EVENTS) and observed_times != sorted(observed_times):
            errors.append("events observed_at 必须按事件顺序单调递增或相等")

    attestation = data.get("attestation")
    if not isinstance(attestation, dict) or set(attestation) != {"method", "confirmed_by_operator"}:
        errors.append("attestation 必须且仅包含 method、confirmed_by_operator")
    elif attestation.get("method") != "operator_attestation" or attestation.get("confirmed_by_operator") is not True:
        errors.append("必须明确标记为 operator_attestation 且由操作者确认")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线检查 WorkBuddy Desktop 连接证据清单（不联网、不读取凭据）")
    parser.add_argument("evidence_file", type=Path, help="JSON evidence manifest 路径")
    parser.add_argument("--allow-loopback-http", action="store_true", help="仅允许 localhost/127.0.0.1/::1 的 HTTP 测试地址")
    args = parser.parse_args(argv)
    try:
        with args.evidence_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"status": "FAIL", "errors": ["证据文件无法读取或不是有效 UTF-8 JSON"]}, ensure_ascii=False))
        return 2

    errors = validate_evidence(data, allow_loopback_http=args.allow_loopback_http)
    result = {
        "status": "PASS" if not errors else "FAIL",
        "checks": {"structural_validation": "passed" if not errors else "failed"},
        "errors": errors,
        "limitations": [
            "PASS 仅表示结构和字段规则通过，不证明截图、客户端事件或 Hub 回执真实",
            "本工具不联网、不启动 WorkBuddy、不读取 Token，也不执行任务",
            "真实连接结论仍需操作者提供客户端现场证据并由独立验收者复核",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
