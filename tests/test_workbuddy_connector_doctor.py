from __future__ import annotations

import json
from pathlib import Path

from scripts.workbuddy_connector_doctor import (
    DEFAULT_PACKAGE,
    EXPECTED_FIELDS,
    validate_environment,
    validate_package,
)


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "connector"
    (root / "skills" / "a2a-workbuddy-pull").mkdir(parents=True)
    (root / "skills" / "a2a-workbuddy-pull" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (root / "icon.svg").write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>", encoding="utf-8")
    (root / "connector-meta.json").write_text(json.dumps({
        "name": "NETIVORA A2A Hub", "source": "netivora-a2a-workbuddy",
        "type": "mcp", "auth_mode": "token", "minWorkbuddyVersion": "4.23.0",
    }), encoding="utf-8")
    (root / "token-schema.json").write_text(json.dumps({
        "fields": [
            {"key": "A2A_HUB_MCP_SERVER_PATH", "type": "text"},
            {"key": "A2A_WORKBUDDY_HUB_URL", "type": "text"},
            {"key": "A2A_WORKBUDDY_CONNECTOR_TOKEN", "type": "password"},
        ]
    }), encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps({
        "mcpServers": {"netivora-a2a-workbuddy": {
            "type": "stdio", "command": "python", "args": ["-c", "import os,runpy; runpy.run_path(os.environ['A2A_HUB_MCP_SERVER_PATH'], run_name='__main__')"],
            "env": {key: f"${{{key}}}" for key in EXPECTED_FIELDS}, "timeout": 30000,
        }}
    }), encoding="utf-8")
    return root


def test_valid_connector_package_passes(tmp_path: Path):
    checks = validate_package(_package(tmp_path))
    assert checks
    assert all(item["status"] == "PASS" for item in checks)


def test_package_rejects_multiple_mcp_servers(tmp_path: Path):
    root = _package(tmp_path)
    config = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    config["mcpServers"]["second"] = config["mcpServers"]["netivora-a2a-workbuddy"]
    (root / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
    assert "mcp-server-count" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_package_rejects_missing_placeholder(tmp_path: Path):
    root = _package(tmp_path)
    config = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    config["mcpServers"]["netivora-a2a-workbuddy"]["env"].pop("A2A_WORKBUDDY_CONNECTOR_TOKEN")
    (root / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
    assert "mcp-placeholders" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_package_requires_password_for_connector_token(tmp_path: Path):
    root = _package(tmp_path)
    schema = json.loads((root / "token-schema.json").read_text(encoding="utf-8"))
    next(x for x in schema["fields"] if x["key"] == "A2A_WORKBUDDY_CONNECTOR_TOKEN")["type"] = "text"
    (root / "token-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    assert "token-secret-type" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_environment_accepts_https_without_echoing_token(tmp_path: Path):
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    secret = "s" * 48
    checks = validate_environment({
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": "https://hub.example.test",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": secret,
    })
    assert all(x["status"] == "PASS" for x in checks)
    assert secret not in json.dumps(checks)


def test_environment_rejects_remote_http(tmp_path: Path):
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    checks = validate_environment({
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": "http://hub.example.test",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "s" * 48,
    })
    assert "hub-url" in {x["id"] for x in checks if x["status"] == "FAIL"}


def test_environment_allows_loopback_http_only_when_opted_in(tmp_path: Path):
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    env = {
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": "http://127.0.0.1:8080",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "s" * 48,
    }
    assert any(x["id"] == "hub-url" and x["status"] == "FAIL" for x in validate_environment(env))
    assert all(x["status"] == "PASS" for x in validate_environment(env, allow_loopback_http=True))


def test_environment_rejects_short_or_whitespace_token(tmp_path: Path):
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    env = {
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": "https://hub.example.test",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "short token",
    }
    assert "connector-token" in {x["id"] for x in validate_environment(env) if x["status"] == "FAIL"}


def test_repository_connector_package_passes():
    checks = validate_package(DEFAULT_PACKAGE)
    assert all(item["status"] == "PASS" for item in checks), checks


def test_package_rejects_duplicate_token_schema_fields(tmp_path: Path):
    root = _package(tmp_path)
    schema = json.loads((root / "token-schema.json").read_text(encoding="utf-8"))
    schema["fields"].append(dict(schema["fields"][-1]))
    (root / "token-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    assert "token-fields" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_package_rejects_placeholder_value_mapped_to_wrong_key(tmp_path: Path):
    root = _package(tmp_path)
    config = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    env = config["mcpServers"]["netivora-a2a-workbuddy"]["env"]
    env["WRONG_FIELD"] = env.pop("A2A_WORKBUDDY_CONNECTOR_TOKEN")
    (root / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
    assert "mcp-placeholders" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_package_rejects_untrusted_mcp_launch_arguments(tmp_path: Path):
    root = _package(tmp_path)
    config = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    config["mcpServers"]["netivora-a2a-workbuddy"]["args"] = ["-c", "import os; os.system('echo unsafe')"]
    (root / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
    assert "mcp-stdio" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def _valid_env(tmp_path: Path, url: str) -> dict[str, str]:
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    return {
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": url,
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "s" * 48,
    }


def test_environment_rejects_hub_url_path_query_userinfo_and_invalid_port(tmp_path: Path):
    for url in (
        "https://hub.example.test/api",
        "https://hub.example.test?token=hidden",
        "https://user:pass@hub.example.test",
        "https://hub.example.test:99999",
    ):
        failures = {x["id"] for x in validate_environment(_valid_env(tmp_path, url)) if x["status"] == "FAIL"}
        assert "hub-url" in failures, url


def test_environment_rejects_malformed_ipv6_and_whitespace(tmp_path: Path):
    for url in ("https://[::1", "https://hub.example.test /"):
        failures = {x["id"] for x in validate_environment(_valid_env(tmp_path, url)) if x["status"] == "FAIL"}
        assert "hub-url" in failures, url


def test_environment_accepts_https_root_with_optional_slash(tmp_path: Path):
    for url in ("https://hub.example.test", "https://hub.example.test/"):
        assert all(x["status"] == "PASS" for x in validate_environment(_valid_env(tmp_path, url)))


def test_package_rejects_empty_or_malformed_minimum_workbuddy_version(tmp_path: Path):
    root = _package(tmp_path)
    meta_path = root / "connector-meta.json"
    for version in ("", "   ", "latest", "4.23"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["minWorkbuddyVersion"] = version
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        failures = {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}
        assert "metadata" in failures, version


def test_package_rejects_unknown_metadata_and_token_schema_fields(tmp_path: Path):
    root = _package(tmp_path)
    meta_path = root / "connector-meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["unexpectedSecret"] = "must-not-be-accepted"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert "metadata-keys" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}

    root = _package(tmp_path)
    schema_path = root / "token-schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["fields"][0]["default"] = "embedded-secret"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    assert "token-field-keys" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_package_rejects_unknown_mcp_fields(tmp_path: Path):
    root = _package(tmp_path)
    config_path = root / "mcp.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["netivora-a2a-workbuddy"]["cwd"] = "/tmp/untrusted"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert "mcp-server-keys" in {x["id"] for x in validate_package(root) if x["status"] == "FAIL"}


def test_environment_non_string_values_fail_closed_without_exception(tmp_path: Path):
    script = tmp_path / "workbuddy_mcp_server.py"
    script.write_text("# test", encoding="utf-8")
    valid = {
        "A2A_HUB_MCP_SERVER_PATH": str(script),
        "A2A_WORKBUDDY_HUB_URL": "https://hub.example.test",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "s" * 48,
    }
    cases = (
        {"A2A_HUB_MCP_SERVER_PATH": 123},
        {"A2A_WORKBUDDY_HUB_URL": 123},
        {"A2A_WORKBUDDY_CONNECTOR_TOKEN": None},
    )
    expected = ("local-script", "hub-url", "connector-token")
    for override, check_id in zip(cases, expected):
        checks = validate_environment({**valid, **override})
        assert check_id in {x["id"] for x in checks if x["status"] == "FAIL"}
        assert all(isinstance(x["status"], str) for x in checks)
