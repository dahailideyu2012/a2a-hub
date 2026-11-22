"""GitHub Issue-comment relay into the restricted ChatGPT Pull ingress.

Only explicitly allowlisted actors can submit a strict /a2a command on an
issue carrying the a2a-relay-enabled label. The comment body is task data;
it is never interpreted as shell or workflow code.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_COMMAND = re.compile(r"^/a2a submit ([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?: pull)?[ \t]*\n+([\s\S]+)$")
_NO_REDIRECT = type(
    "NoRedirect",
    (urllib.request.HTTPRedirectHandler,),
    {"redirect_request": lambda self, req, fp, code, msg, headers, newurl: None},
)


class RelayError(RuntimeError):
    pass


def parse_command(body: str) -> tuple[str, str]:
    if not isinstance(body, str):
        raise RelayError("comment body must be text")
    match = _COMMAND.fullmatch(body.strip())
    if not match:
        raise RelayError("expected: /a2a submit <agent-id> followed by a blank line and prompt")
    agent_id, prompt = match.group(1), match.group(2).strip()
    if not prompt or len(prompt) > 12000:
        raise RelayError("prompt must contain 1-12000 characters")
    return agent_id, prompt


def _csv_env(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


def _validate_event(event: dict[str, Any], actor_allowlist: set[str], agent_allowlist: set[str]) -> tuple[str, str, str]:
    if event.get("action") != "created":
        raise RelayError("only newly created comments are accepted")
    issue = event.get("issue")
    if not isinstance(issue, dict) or issue.get("pull_request"):
        raise RelayError("relay accepts issue comments only, not pull-request comments")
    raw_labels = issue.get("labels")
    if not isinstance(raw_labels, list):
        raise RelayError("issue labels are missing or invalid")
    labels = {
        (item.get("name") or "").lower()
        for item in raw_labels
        if isinstance(item, dict)
    }
    if "a2a-relay-enabled" not in labels:
        raise RelayError("issue is not enabled for A2A relay")
    comment = event.get("comment")
    if not isinstance(comment, dict):
        raise RelayError("GitHub comment payload is missing")
    user = comment.get("user")
    actor = user.get("login", "") if isinstance(user, dict) else ""
    if not actor or actor not in actor_allowlist:
        raise RelayError("comment author is not allowlisted")
    agent_id, prompt = parse_command(comment.get("body", ""))
    if agent_id not in agent_allowlist:
        raise RelayError("agent is not allowlisted for GitHub relay")
    repo = event.get("repository") or {}
    repo_id = repo.get("id")
    issue_number = issue.get("number")
    comment_id = comment.get("id")
    if not all(isinstance(v, int) and v > 0 for v in (repo_id, issue_number, comment_id)):
        raise RelayError("GitHub event is missing stable numeric identifiers")
    # Stable across retries and workflow re-runs; unique per GitHub comment.
    key = f"gh-{repo_id}-{issue_number}-{comment_id}"
    return agent_id, prompt, key


def signature(secret: str, timestamp: str, idempotency_key: str, raw_body: bytes) -> str:
    message = timestamp.encode("ascii") + b"." + idempotency_key.encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def _post_hub(url: str, token: str, secret: str, key: str, payload: dict[str, str]) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RelayError("A2A_HUB_URL must be a clean HTTPS origin/base URL")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {
        "X-ChatGPT-Ingress-Token": token,
        "Idempotency-Key": key,
        "X-A2A-Relay-Source": "github-issue-comment",
        "X-A2A-Relay-Timestamp": timestamp,
        "X-A2A-Relay-Signature": signature(secret, timestamp, key, raw),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/integrations/chatgpt/v1/tasks",
        data=raw,
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(_NO_REDIRECT())
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=15) as response:
                data = json.loads(response.read(256 * 1024 + 1).decode("utf-8"))
                if not isinstance(data, dict):
                    raise RelayError("Hub returned a non-object response")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
                continue
            raise RelayError(f"Hub rejected relay request (HTTP {exc.code})") from None
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            break
    raise RelayError("Hub request failed after safe idempotent retries") from last_error


def _post_github_comment(event: dict[str, Any], token: str, body: str) -> None:
    repo = event["repository"]["full_name"]
    issue_number = event["issue"]["number"]
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    raw = json.dumps({"body": body}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.build_opener(_NO_REDIRECT()).open(request, timeout=15) as response:
        if response.status not in (200, 201):
            raise RelayError("GitHub comment API returned an unexpected status")


def main() -> int:
    try:
        event_path = os.environ["GITHUB_EVENT_PATH"]
        with open(event_path, "r", encoding="utf-8") as handle:
            event = json.load(handle)
        actors = _csv_env("A2A_GITHUB_RELAY_ALLOWED_ACTORS")
        agents = _csv_env("A2A_GITHUB_RELAY_ALLOWED_AGENTS")
        if not actors or not agents:
            raise RelayError("actor and agent allowlists must both be configured")
        agent_id, prompt, key = _validate_event(event, actors, agents)
        hub_url = os.environ.get("A2A_HUB_URL", "").strip()
        ingress_token = os.environ.get("A2A_CHATGPT_INGRESS_TOKEN", "")
        signing_secret = os.environ.get("A2A_GITHUB_RELAY_HMAC_SECRET", "")
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if len(ingress_token) < 32 or len(signing_secret) < 32 or not github_token:
            raise RelayError("required relay credentials are missing or too short")
        result = _post_hub(
            hub_url,
            ingress_token,
            signing_secret,
            key,
            {"agent_id": agent_id, "prompt": prompt, "mode": "pull"},
        )
        task = result.get("task") if isinstance(result, dict) else None
        task_id = task.get("id") if isinstance(task, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise RelayError("Hub response did not contain a task ID")
        replay = bool(result.get("idempotent_replay"))
        _post_github_comment(
            event,
            github_token,
            f"**A2A Hub 已接收任务**\n\n- Task: `{task_id}`\n- Agent: `{agent_id}`\n- Mode: `pull`\n- Idempotent replay: `{str(replay).lower()}`\n\nWorkBuddy 可从 Hub Pull 队列领取。此消息不代表任务已执行完成。",
        )
        print(f"accepted task_id={task_id} replay={replay}")
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError, RelayError, urllib.error.URLError) as exc:
        # Never print credentials, request headers, or the user prompt.
        print(f"relay rejected: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
