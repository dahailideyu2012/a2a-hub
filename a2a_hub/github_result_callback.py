"""Idempotent GitHub Issue result delivery for signed relay-originated tasks."""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NO_REDIRECT = type(
    "NoRedirect",
    (urllib.request.HTTPRedirectHandler,),
    {"redirect_request": lambda self, req, fp, code, msg, headers, newurl: None},
)


class GithubResultDeliveryError(RuntimeError):
    """A retryable GitHub callback failure; never contains credentials or output."""


def _marker(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", task_id)[:100]
    if not safe:
        raise GithubResultDeliveryError("invalid task identifier")
    return f"<!-- a2a-hub-result:{safe} -->"


def render_result_comment(task_id: str, state: str, output: str) -> str:
    if state not in {"completed", "failed"}:
        raise GithubResultDeliveryError("unsupported terminal task state")
    # Treat agent output as inert text: escape HTML, suppress @-mentions, and cap size.
    bounded = (output or "(empty output)")[:8000]
    bounded = bounded.replace("@", "@&#8203;")
    escaped = html.escape(bounded, quote=False)
    return (
        f"{_marker(task_id)}\n"
        f"## A2A Hub 执行结果\n\n"
        f"- Task: `{task_id}`\n"
        f"- Status: **{state}**\n\n"
        f"<details><summary>查看 WorkBuddy 输出</summary>\n\n"
        f"<pre>{escaped}</pre>\n\n</details>\n"
    )


def _request(url: str, token: str, method: str, body: dict[str, Any] | None = None) -> Any:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if raw is not None else {}),
        },
        method=method,
    )
    opener = urllib.request.build_opener(_NO_REDIRECT())
    try:
        with opener.open(request, timeout=12) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise GithubResultDeliveryError("GitHub API response exceeded size limit")
            if response.status not in (200, 201):
                raise GithubResultDeliveryError("GitHub API returned an unexpected status")
            return json.loads(payload.decode("utf-8")) if payload else {}
    except urllib.error.HTTPError as exc:
        # Do not expose response bodies, headers, token, or agent output in errors.
        raise GithubResultDeliveryError(f"GitHub API request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise GithubResultDeliveryError("GitHub API request failed; retry the same WorkBuddy result") from None


def deliver_github_issue_result(
    callback: dict[str, Any], task_id: str, state: str, output: str,
    api_token: str, allowed_repos: str,
) -> dict[str, Any]:
    repository = callback.get("repository")
    issue_number = callback.get("issue_number")
    if not isinstance(repository, str) or not _REPO_RE.fullmatch(repository):
        raise GithubResultDeliveryError("invalid callback repository")
    allowed = {item.strip().lower() for item in allowed_repos.split(",") if item.strip()}
    if repository.lower() not in allowed:
        raise GithubResultDeliveryError("callback repository is not allowlisted")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
        raise GithubResultDeliveryError("invalid callback issue number")
    if len(api_token) < 20:
        raise GithubResultDeliveryError("GitHub callback token is not configured")

    base = "https://api.github.com/repos/" + repository + "/issues/" + str(issue_number) + "/comments"
    marker = _marker(task_id)
    # Read-before-write makes retries idempotent, including the case where GitHub
    # accepted a POST but the response was lost. Bound pagination to 10,000 comments.
    for page in range(1, 101):
        url = base + "?" + urllib.parse.urlencode({"per_page": 100, "page": page})
        comments = _request(url, api_token, "GET")
        if not isinstance(comments, list):
            raise GithubResultDeliveryError("GitHub comments response is invalid")
        for item in comments:
            if isinstance(item, dict) and marker in str(item.get("body", "")):
                return {"delivered": True, "replayed": True}
        if len(comments) < 100:
            break
    else:
        raise GithubResultDeliveryError("GitHub comment history exceeded safe pagination limit")

    body = render_result_comment(task_id, state, output)
    created = _request(base, api_token, "POST", {"body": body})
    comment_url = created.get("html_url") if isinstance(created, dict) else None
    return {"delivered": True, "replayed": False, "comment_url": comment_url}
