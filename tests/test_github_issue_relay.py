from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "github_issue_relay.py"
SPEC = importlib.util.spec_from_file_location("github_issue_relay", MODULE_PATH)
relay = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(relay)


def event(*, actor="owner", body="/a2a submit echo-a\\n\\nhello Hub", labels=None, is_pr=False):
    selected_labels = ["a2a-relay-enabled"] if labels is None else labels
    issue = {"number": 5, "labels": [{"name": name} for name in selected_labels]}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/pulls/5"}
    return {
        "action": "created",
        "repository": {"id": 1234, "full_name": "owner/repo"},
        "issue": issue,
        "comment": {"id": 5678, "body": body, "user": {"login": actor, "type": "User"}},
    }


def test_parse_command_and_default_pull_mode():
    assert relay.parse_command("/a2a submit echo-a\\n\\nhello\\nworld") == ("echo-a", "hello\\nworld")


@pytest.mark.parametrize("body", [
    "hello",
    "/a2a submit echo-a",
    "/a2a submit echo-a\\n\\n",
    "/a2a submit echo-a; rm -rf /\\n\\nprompt",
])
def test_rejects_noncanonical_command_shapes(body):
    with pytest.raises(relay.RelayError):
        relay.parse_command(body)


def test_prompt_treats_command_like_lines_as_data_not_executable_commands():
    assert relay.parse_command("/a2a submit echo-a\\n\\nordinary prompt\\n/a2a submit echo-b") == (
        "echo-a", "ordinary prompt\\n/a2a submit echo-b"
    )


def test_event_requires_allowlisted_actor_agent_and_label():
    assert relay._validate_event(event(), {"owner"}, {"echo-a"}) == ("echo-a", "hello Hub", "gh-1234-5-5678")
    with pytest.raises(relay.RelayError):
        relay._validate_event(event(actor="stranger"), {"owner"}, {"echo-a"})
    with pytest.raises(relay.RelayError):
        relay._validate_event(event(), {"owner"}, {"echo-b"})
    with pytest.raises(relay.RelayError):
        relay._validate_event(event(labels=[]), {"owner"}, {"echo-a"})
    with pytest.raises(relay.RelayError):
        relay._validate_event(event(is_pr=True), {"owner"}, {"echo-a"})


def test_idempotency_key_is_stable_for_same_comment():
    first = relay._validate_event(event(), {"owner"}, {"echo-a"})[2]
    second = relay._validate_event(event(), {"owner"}, {"echo-a"})[2]
    assert first == second


def test_hmac_signature_is_bound_to_timestamp_key_and_body():
    secret = "s" * 40
    body = b'{"agent_id":"echo-a","mode":"pull","prompt":"hello"}'
    sig = relay.signature(secret, "1700000000", "gh-1-2-3", body)
    assert sig.startswith("sha256=")
    assert sig != relay.signature(secret, "1700000001", "gh-1-2-3", body)
    assert sig != relay.signature(secret, "1700000000", "gh-1-2-4", body)
    assert sig != relay.signature(secret, "1700000000", "gh-1-2-3", body + b" ")
