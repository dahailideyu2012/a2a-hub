#!/usr/bin/env python3
"""Static and local-environment preflight for the WorkBuddy A2A Hub Connector.

This command never prints credential values and never sends network requests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = ROOT / "connectors" / "netivora-a2a-workbuddy"
EXPECTED_FIELDS = {
    "A2A_HUB_MCP_SERVER_PATH",
    "A2A_WORKBUDDY_HUB_URL",
    "A2A_WORKBUDDY_CONNECTOR_TOKEN",
}
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
VERSION = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
def _is_semver(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = VERSION.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(1)
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if not any(char.isalnum() for char in identifier):
                return False
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                return False
    return True


EXPECTED_FIELD_TYPES = {
    "A2A_HUB_MCP_SERVER_PATH": "text",
    "A2A_WORKBUDDY_HUB_URL": "text",
    "A2A_WORKBUDDY_CONNECTOR_TOKEN": "password",
}
META_KEYS = {
    "name", "name_zh", "name_en", "description", "description_zh", "description_en",
    "source", "type", "auth_mode", "version", "minWorkbuddyVersion",
    "examples_zh", "examples_en",
}
TOKEN_SCHEMA_KEYS = {"title", "title_en", "description", "description_en", "docUrl", "docLabel", "fields"}
TOKEN_FIELD_KEYS = {"key", "label", "label_en", "type", "required", "description", "description_en", "placeholder"}
MCP_ROOT_KEYS = {"mcpServers"}
MCP_SERVER_KEYS = {"type", "command", "args", "env", "timeout"}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys instead of silently accepting the last value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json(path: Path, checks: list[dict[str, str]]) -> dict[str, Any]:
    if not path.is_file():
        checks.append({"id": f"file:{path.name}", "status": "FAIL", "message": f"缺少文件：{path.name}"})
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, ValueError):
        checks.append({"id": f"json:{path.name}", "status": "FAIL", "message": f"{path.name} 不是有效 UTF-8 JSON"})
        return {}
    if not isinstance(value, dict):
        checks.append({"id": f"json-root:{path.name}", "status": "FAIL", "message": f"{path.name} 顶层必须是 JSON 对象"})
        return {}
    checks.append({"id": f"json:{path.name}", "status": "PASS", "message": f"{path.name} 格式有效"})
    return value


def _check(checks: list[dict[str, str]], check_id: str, ok: bool, passed: str, failed: str) -> None:
    checks.append({"id": check_id, "status": "PASS" if ok else "FAIL", "message": passed if ok else failed})


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def validate_package(package_dir: Path | str = DEFAULT_PACKAGE) -> list[dict[str, str]]:
    """Validate the WorkBuddy Connector package without reading user credentials."""
    package = Path(package_dir)
    checks: list[dict[str, str]] = []
    meta = _read_json(package / "connector-meta.json", checks)
    mcp = _read_json(package / "mcp.json", checks)
    token_schema = _read_json(package / "token-schema.json", checks)

    _check(checks, "skill", (package / "skills" / "a2a-workbuddy-pull" / "SKILL.md").is_file(),
           "Skill 文件存在", "缺少 skills/a2a-workbuddy-pull/SKILL.md")
    _check(checks, "icon", (package / "icon.svg").is_file(), "连接器图标存在", "缺少 icon.svg")

    source = meta.get("source")
    min_version = meta.get("minWorkbuddyVersion")
    _check(checks, "metadata-keys", set(meta).issubset(META_KEYS),
           "连接器元信息没有未知字段", "connector-meta.json 包含未识别字段")
    package_version = meta.get("version")
    _check(checks, "metadata", meta.get("type") == "mcp" and meta.get("auth_mode") == "token"
           and isinstance(source, str) and bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source))
           and _is_semver(min_version)
           and (package_version is None or _is_semver(package_version)),
           "连接器元信息包含 MCP/Token 模式、合法 source 和有效 SemVer 版本",
           "connector-meta.json 的 type/auth_mode/source/version/minWorkbuddyVersion 不符合规范")

    _check(checks, "token-schema-keys", set(token_schema).issubset(TOKEN_SCHEMA_KEYS),
           "凭据表单没有未知顶层字段", "token-schema.json 包含未识别顶层字段")
    fields = token_schema.get("fields")
    field_objects = fields if isinstance(fields, list) and all(isinstance(item, dict) for item in fields) else []
    field_keys = [item.get("key") for item in field_objects]
    _check(checks, "token-fields", isinstance(fields, list) and len(fields) == 3 and len(field_keys) == 3 and set(field_keys) == EXPECTED_FIELDS,
           "凭据表单包含且仅包含三个必需字段，且没有重复项",
           "token-schema.json 必须恰好包含三个不重复的必需字段")
    _check(checks, "token-field-keys", len(field_objects) == 3 and all(set(item).issubset(TOKEN_FIELD_KEYS) for item in field_objects),
           "凭据字段没有未知属性", "token-schema.json 的字段对象包含未识别属性")
    field_contract_ok = (
        len(field_objects) == 3
        and all(
            item.get("type") == EXPECTED_FIELD_TYPES.get(item.get("key"))
            and item.get("required") is True
            and isinstance(item.get("label"), str) and bool(item["label"].strip())
            and isinstance(item.get("label_en"), str) and bool(item["label_en"].strip())
            for item in field_objects
        )
    )
    _check(checks, "token-field-contract", field_contract_ok,
           "三个凭据字段的类型、必填状态和双语标签符合契约",
           "token-schema.json 字段类型/required/label 契约不符合要求")
    token_field = next((item for item in fields if isinstance(item, dict) and item.get("key") == "A2A_WORKBUDDY_CONNECTOR_TOKEN"), {}) if isinstance(fields, list) else {}
    _check(checks, "token-secret-type", token_field.get("type") == "password",
           "Connector Token 使用 password 输入类型", "Connector Token 字段必须使用 password 类型")

    _check(checks, "mcp-root-keys", set(mcp).issubset(MCP_ROOT_KEYS),
           "MCP 配置没有未知顶层字段", "mcp.json 包含未识别顶层字段")
    servers = mcp.get("mcpServers")
    server_values = list(servers.values()) if isinstance(servers, dict) else []
    _check(checks, "mcp-server-count", isinstance(servers, dict) and list(servers) == ["netivora-a2a-workbuddy"],
           "mcp.json 仅包含预期的 NETIVORA MCP Server",
           "mcp.json 必须且只能包含 netivora-a2a-workbuddy")
    server = server_values[0] if len(server_values) == 1 and isinstance(server_values[0], dict) else {}
    _check(checks, "mcp-server-keys", bool(server) and set(server).issubset(MCP_SERVER_KEYS),
           "MCP Server 配置没有未知属性", "MCP Server 配置包含未识别属性")
    expected_args = [
        "-c",
        "import os,runpy; runpy.run_path(os.environ['A2A_HUB_MCP_SERVER_PATH'], run_name='__main__')",
    ]
    _check(checks, "mcp-stdio", server.get("type") == "stdio" and server.get("command") == "python"
           and server.get("args") == expected_args and server.get("timeout") == 30000,
           "MCP 使用固定的 Python stdio 启动器和受控脚本路径",
           "MCP 必须使用预期 Python stdio 启动命令，不得注入任意命令或参数")
    env_map = server.get("env") if isinstance(server.get("env"), dict) else {}
    expected_env = {key: f"${{{key}}}" for key in EXPECTED_FIELDS}
    _check(checks, "mcp-placeholders", env_map == expected_env,
           "三个用户配置字段逐项精确映射为占位符，未硬编码凭据",
           "mcp.json env 必须仅包含三个字段及其同名占位符")
    return checks


def validate_environment(env: dict[str, Any] | None = None, allow_loopback_http: bool = False) -> list[dict[str, str]]:
    """Check required local settings; malformed programmatic values fail closed."""
    values = os.environ if env is None else env
    checks: list[dict[str, str]] = []
    raw_script = values.get("A2A_HUB_MCP_SERVER_PATH", "")
    script_value = raw_script.strip() if isinstance(raw_script, str) else ""
    script_path = Path(script_value).expanduser() if script_value else None
    _check(checks, "local-script", bool(script_path and script_path.is_file() and not script_path.is_symlink() and script_path.name == "workbuddy_mcp_server.py"),
           "本机 MCP 脚本路径存在且文件名正确", "A2A_HUB_MCP_SERVER_PATH 必须指向存在的 workbuddy_mcp_server.py")

    raw_hub_url = values.get("A2A_WORKBUDDY_HUB_URL", "")
    hub_url = raw_hub_url.strip() if isinstance(raw_hub_url, str) else ""
    try:
        parsed = urlsplit(hub_url)
        hostname = parsed.hostname
        port = parsed.port  # Access validates malformed and out-of-range ports.
        loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        scheme_ok = parsed.scheme == "https" or (allow_loopback_http and parsed.scheme == "http" and loopback)
        url_ok = bool(
            hub_url
            and not any(ch.isspace() for ch in hub_url)
            and scheme_ok
            and parsed.netloc
            and hostname
            and (port is None or 1 <= port <= 65535)
            and parsed.path in {"", "/"}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        url_ok = False
    _check(checks, "hub-url", url_ok,
           "Hub 地址格式有效（不会发起网络请求）",
           "A2A_WORKBUDDY_HUB_URL 必须是 HTTPS 根地址；仅显式允许时可使用 localhost HTTP，且不得含凭据、查询或片段")

    raw_token = values.get("A2A_WORKBUDDY_CONNECTOR_TOKEN", "")
    token = raw_token if isinstance(raw_token, str) else ""
    token_ok = len(token) >= 32 and not any(ch.isspace() for ch in token)
    _check(checks, "connector-token", token_ok,
           "独立 Connector Token 已配置且长度符合要求（值已隐藏）",
           "A2A_WORKBUDDY_CONNECTOR_TOKEN 必须至少 32 个字符且不含空白")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 NETIVORA A2A Hub WorkBuddy Connector 包和本机配置")
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE, help="Connector 包目录")
    parser.add_argument("--check-env", action="store_true", help="额外检查当前进程的本机环境变量；不发起网络请求")
    parser.add_argument("--allow-loopback-http", action="store_true", help="仅用于本机测试，允许 localhost HTTP")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    checks = validate_package(args.package_dir)
    if args.check_env:
        checks.extend(validate_environment(allow_loopback_http=args.allow_loopback_http))
    failed = sum(item["status"] == "FAIL" for item in checks)
    summary = {
        "schema_version": 1,
        "status": "FAIL" if failed else "PASS",
        "passed": len(checks) - failed,
        "failed": failed,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"[{item['status']}] {item['id']}: {item['message']}")
        print(f"\n结果：{summary['status']}（通过 {summary['passed']}，失败 {summary['failed']}）")
        if args.check_env:
            print("说明：本命令只做静态/本机检查，不会连接 Hub，也不代表 WorkBuddy Desktop 已在线。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
