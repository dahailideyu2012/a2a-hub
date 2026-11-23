#!/usr/bin/env python3
"""Read-only observer for a manually initiated GitHub -> Hub -> WorkBuddy round trip.

Default mode is offline and performs no network I/O. Live observation requires
an explicit flag, a dedicated read-only GitHub token, and an exact sandbox repo.
The observer never creates comments, dispatches workflows, or prints comment bodies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TASK_RE = re.compile(r"(?m)^- Task: `([A-Za-z0-9._:-]{8,128})`$")
_MARKER_RE = re.compile(r"<!-- a2a-hub-result:([A-Za-z0-9_-]{1,100}) -->")
_NO_REDIRECT = type(
    "NoRedirect",
    (urllib.request.HTTPRedirectHandler,),
    {"redirect_request": lambda self, req, fp, code, msg, headers, newurl: None},
)


class ObserverError(RuntimeError):
    """Safe, non-secret observer failure."""


def _request_json(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "a2a-hub-live-observer",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NO_REDIRECT())
    try:
        with opener.open(request, timeout=12) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ObserverError("GitHub response exceeded the 1 MiB safety limit")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ObserverError(f"GitHub read request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise ObserverError("GitHub read request failed; no write was attempted") from None


def _marker_key(task_id: str) -> str:
    """Mirror github_result_callback._marker, including its 100-char cap."""
    return re.sub(r"[^A-Za-z0-9_-]", "", task_id)[:100]


def observe(repository: str, issue_number: int, token: str, allowed_repository: str) -> dict[str, Any]:
    if not _REPO_RE.fullmatch(repository):
        raise ObserverError("repository must be an exact owner/repo value")
    if repository.casefold() != allowed_repository.casefold():
        raise ObserverError("repository does not match the dedicated sandbox repository allowlist")
    if issue_number <= 0:
        raise ObserverError("issue number must be positive")
    if len(token) < 20:
        raise ObserverError("dedicated GitHub read token is missing or too short")

    base = "https://api.github.com/repos/" + repository
    issue = _request_json(base + "/issues/" + str(issue_number), token)
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise ObserverError("target must be a GitHub Issue, not a pull request")
    if issue.get("state") != "open":
        raise ObserverError("sandbox Issue must remain open during observation")
    labels = {
        str(item.get("name", "")).casefold()
        for item in issue.get("labels", [])
        if isinstance(item, dict)
    }
    if "a2a-relay-enabled" not in labels:
        raise ObserverError("sandbox Issue is missing the a2a-relay-enabled label")

    comments: list[dict[str, Any]] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        batch = _request_json(base + "/issues/" + str(issue_number) + "/comments?" + query, token)
        if not isinstance(batch, list):
            raise ObserverError("GitHub comments response is invalid")
        comments.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    else:
        raise ObserverError("comment history exceeded the 1,000-comment observation limit")

    accepted: list[tuple[int, str]] = []
    completed: set[str] = set()
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        task_match = _TASK_RE.search(body)
        if task_match and "A2A Hub 已接收任务" in body:
            accepted.append((int(comment.get("id") or 0), task_match.group(1)))
        completed.update(_MARKER_RE.findall(body))

    accepted.sort(key=lambda item: item[0])
    if not accepted:
        state, task_id = "WAITING_FOR_RELAY", None
    else:
        task_id = accepted[-1][1]
        state = "ROUNDTRIP_CONFIRMED" if _marker_key(task_id) in completed else "WAITING_FOR_WORKBUDDY"

    return {
        "suite": "a2a-hub-live-integration-observer",
        "result": state,
        "repository": repository,
        "issue_number": issue_number,
        "task_id": task_id,
        "github_reads_only": True,
        "github_writes": 0,
        "comments_scanned": len(comments),
        "limitations": [
            "This observer reads GitHub Issue metadata and comments only.",
            "A matching result marker confirms the callback comment exists; it does not independently attest to WorkBuddy desktop internals.",
            "No task prompt, comment body, token, or agent output is printed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="exact sandbox owner/repo")
    parser.add_argument("--issue", required=True, type=int, help="disposable open sandbox Issue number")
    parser.add_argument("--observe", action="store_true", help="enable read-only GitHub observation")
    parser.add_argument("--confirm-read-only-github", action="store_true",
                        help="acknowledge that this command will make GitHub GET requests only")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    if not args.observe:
        report = {
            "suite": "a2a-hub-live-integration-observer",
            "result": "DRY_RUN",
            "repository": args.repo,
            "issue_number": args.issue,
            "network_access": "none",
            "github_writes": 0,
            "next_step": "Review the exact sandbox target, then rerun with --observe --confirm-read-only-github.",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
              f"DRY_RUN: no network access; target={args.repo}#{args.issue}")
        return 0

    if not args.confirm_read_only_github:
        print("BLOCKED: --observe requires --confirm-read-only-github", file=sys.stderr)
        return 2

    try:
        allowed = os.environ.get("A2A_LIVE_TEST_REPOSITORY", "").strip()
        token = os.environ.get("A2A_LIVE_GITHUB_READ_TOKEN", "")
        report = observe(args.repo, args.issue, token, allowed)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
              f"{report['result']}: {args.repo}#{args.issue} task={report['task_id'] or 'not-yet-accepted'}")
        return 0 if report["result"] == "ROUNDTRIP_CONFIRMED" else 3
    except ObserverError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
