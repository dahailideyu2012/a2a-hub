#!/usr/bin/env python3
"""Offline preflight for a controlled A2A Hub live-integration environment.

This command never makes network requests and never prints credentials.
It checks configuration readiness only; it does not prove that external
GitHub or WorkBuddy services are reachable or that credentials have scopes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from a2a_hub.config import ROOT, Settings

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _csv(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _resolved(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def evaluate(settings: Settings) -> dict:
    checks: list[dict[str, str | bool]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    ingress_agents = _csv(settings.chatgpt_ingress_allowed_agents)
    workbuddy_agents = _csv(settings.workbuddy_connector_allowed_agents)
    shared_agents = ingress_agents & workbuddy_agents
    repositories = _csv(settings.github_relay_allowed_repos)
    parsed_url = urlsplit(settings.public_url)
    https_url = (
        parsed_url.scheme == "https"
        and bool(parsed_url.hostname)
        and parsed_url.username is None
        and parsed_url.password is None
        and not parsed_url.query
        and not parsed_url.fragment
    )

    add("sqlite_task_store", settings.store.lower() == "sqlite",
        "A2A_STORE must be sqlite for durable Pull tasks")
    add("general_api_auth", settings.require_auth and len(settings.api_token) >= 32,
        "A2A_REQUIRE_AUTH=true and A2A_API_TOKEN must contain at least 32 characters")
    add("public_https_url", https_url,
        "A2A_PUBLIC_URL must be HTTPS without embedded credentials, query, or fragment")
    add("chatgpt_ingress_token", len(settings.chatgpt_ingress_token) >= 32,
        "dedicated ChatGPT ingress token is present and meets the 32-character minimum")
    add("workbuddy_connector_token", len(settings.workbuddy_connector_token) >= 32,
        "dedicated WorkBuddy connector token is present and meets the 32-character minimum")
    add("independent_tokens",
        len({settings.api_token, settings.chatgpt_ingress_token,
             settings.workbuddy_connector_token, settings.github_relay_hmac_secret,
             settings.github_relay_api_token}) == 5
        and all((settings.api_token, settings.chatgpt_ingress_token,
                 settings.workbuddy_connector_token, settings.github_relay_hmac_secret,
                 settings.github_relay_api_token)),
        "API, ChatGPT ingress, WorkBuddy, GitHub HMAC, and GitHub API credentials are all present and distinct")
    add("agent_allowlist_overlap", bool(shared_agents),
        "ChatGPT ingress and WorkBuddy connector must allow at least one common agent")
    add("github_hmac_secret", len(settings.github_relay_hmac_secret) >= 32,
        "GitHub relay HMAC secret meets the 32-character minimum")
    add("github_api_token", len(settings.github_relay_api_token) >= 20,
        "GitHub result callback token is present (token scope is not verified offline)")
    invalid_repos = sorted(repo for repo in repositories if not _REPO.fullmatch(repo))
    add("github_repository_allowlist", bool(repositories) and not invalid_repos,
        "one or more exact owner/repo entries are configured" if repositories and not invalid_repos
        else "A2A_GITHUB_RELAY_ALLOWED_REPOS must contain exact owner/repo entries")
    task_db = _resolved(settings.db_path)
    ingress_db = settings.chatgpt_ingress_db_path_resolved()
    outbox_db = _resolved(settings.github_callback_outbox_db_path)
    distinct_paths = len({str(task_db), str(ingress_db), str(outbox_db)}) == 3
    add("separate_sqlite_databases", distinct_paths,
        "task, ingress-ledger, and callback-outbox databases resolve to separate paths")
    add("durable_outbox_settings",
        settings.github_callback_outbox_max_attempts >= 1
        and settings.github_callback_outbox_poll_seconds >= 0.1,
        "callback outbox retry and polling settings are within configured bounds")

    passed = all(item["passed"] for item in checks)
    return {
        "suite": "a2a-hub-live-integration-preflight",
        "result": "READY" if passed else "BLOCKED",
        "network_access": "none",
        "credentials_printed": False,
        "configured_agent_overlap": sorted(shared_agents),
        "configured_repository_allowlist": sorted(repositories),
        "database_paths_distinct": distinct_paths,
        "checks": checks,
        "limitations": [
            "This is configuration preflight only; it does not call GitHub or WorkBuddy.",
            "It cannot verify GitHub token permissions, WorkBuddy desktop connectivity, DNS, TLS, or public reachability.",
            "A READY result is not authorization to write to GitHub or deploy."
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    report = evaluate(Settings())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['suite']}: {report['result']}")
        for item in report["checks"]:
            print(f"{'PASS' if item['passed'] else 'BLOCKED'}  {item['name']}: {item['detail']}")
        for note in report["limitations"]:
            print(f"NOTE  {note}")
    return 0 if report["result"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
