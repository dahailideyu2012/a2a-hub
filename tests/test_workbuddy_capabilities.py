"""T71-T77 WorkBuddy capability negotiation contract tests."""
from __future__ import annotations

import pytest

from a2a_hub.workbuddy_capabilities import (
    MAX_CAPABILITIES,
    SUPPORTED_CONNECTOR_CAPABILITIES,
    negotiate_capabilities,
)


def test_negotiation_is_sorted_deduplicated_by_server_allowlist():
    negotiated, unsupported = negotiate_capabilities(
        ["result", "future_feature", "claim", "heartbeat"]
    )
    assert negotiated == ["claim", "heartbeat", "result"]
    assert unsupported == ["future_feature"]


def test_empty_capability_list_is_backward_compatible():
    assert negotiate_capabilities([]) == ([], [])


@pytest.mark.parametrize("value", [
    "claim", ["Claim"], ["bad-name"], ["../claim"], [""], [None], [1],
])
def test_malformed_capabilities_fail_closed(value):
    with pytest.raises(ValueError):
        negotiate_capabilities(value)


def test_duplicate_capability_fails_closed():
    with pytest.raises(ValueError, match="duplicate"):
        negotiate_capabilities(["claim", "claim"])


def test_capability_count_is_bounded():
    names = [f"capability_{index}" for index in range(MAX_CAPABILITIES + 1)]
    with pytest.raises(ValueError, match="too many"):
        negotiate_capabilities(names)


def test_server_capability_catalog_is_explicit_and_stable():
    assert SUPPORTED_CONNECTOR_CAPABILITIES == (
        "claim", "heartbeat", "progress", "renew", "result", "start"
    )
