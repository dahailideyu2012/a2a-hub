from __future__ import annotations

from a2a_hub.config import Settings
from scripts.live_integration_preflight import evaluate


def configured_settings(**overrides):
    values = {
        "A2A_STORE": "sqlite",
        "A2A_REQUIRE_AUTH": "true",
        "A2A_API_TOKEN": "api-token-" + "a" * 32,
        "A2A_PUBLIC_URL": "https://hub.example.test",
        "A2A_CHATGPT_INGRESS_TOKEN": "chatgpt-token-" + "b" * 32,
        "A2A_CHATGPT_INGRESS_ALLOWED_AGENTS": "echo-a,workbuddy",
        "A2A_WORKBUDDY_CONNECTOR_TOKEN": "workbuddy-token-" + "c" * 32,
        "A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS": "workbuddy",
        "A2A_GITHUB_RELAY_HMAC_SECRET": "hmac-secret-" + "d" * 32,
        "A2A_GITHUB_RELAY_API_TOKEN": "github-token-" + "e" * 24,
        "A2A_GITHUB_RELAY_ALLOWED_REPOS": "owner/sandbox",
        "A2A_DB_PATH": "./data/tasks.sqlite3",
        "A2A_CHATGPT_INGRESS_DB_PATH": "./data/ingress.sqlite3",
        "A2A_GITHUB_CALLBACK_OUTBOX_DB_PATH": "./data/outbox.sqlite3",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_complete_live_configuration_is_ready_without_network_calls():
    report = evaluate(configured_settings())
    assert report["result"] == "READY"
    assert report["network_access"] == "none"
    assert report["credentials_printed"] is False
    assert report["configured_agent_overlap"] == ["workbuddy"]
    assert all(item["passed"] for item in report["checks"])


def test_missing_workbuddy_agent_overlap_blocks_preflight():
    report = evaluate(configured_settings(A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS="echo-b"))
    assert report["result"] == "BLOCKED"
    assert not next(item for item in report["checks"] if item["name"] == "agent_allowlist_overlap")["passed"]


def test_http_public_url_blocks_live_preflight():
    report = evaluate(configured_settings(A2A_PUBLIC_URL="http://hub.example.test"))
    assert report["result"] == "BLOCKED"
    assert not next(item for item in report["checks"] if item["name"] == "public_https_url")["passed"]


def test_reused_tokens_block_preflight():
    token = "same-token-" + "x" * 32
    report = evaluate(configured_settings(
        A2A_API_TOKEN=token,
        A2A_CHATGPT_INGRESS_TOKEN=token,
    ))
    assert report["result"] == "BLOCKED"
    assert not next(item for item in report["checks"] if item["name"] == "independent_tokens")["passed"]


def test_shared_sqlite_path_blocks_preflight():
    report = evaluate(configured_settings(
        A2A_GITHUB_CALLBACK_OUTBOX_DB_PATH="./data/tasks.sqlite3"
    ))
    assert report["result"] == "BLOCKED"
    assert not next(item for item in report["checks"] if item["name"] == "separate_sqlite_databases")["passed"]


def test_invalid_repository_allowlist_blocks_preflight():
    report = evaluate(configured_settings(A2A_GITHUB_RELAY_ALLOWED_REPOS="owner;not-a-repo"))
    assert report["result"] == "BLOCKED"
    assert not next(item for item in report["checks"] if item["name"] == "github_repository_allowlist")["passed"]
