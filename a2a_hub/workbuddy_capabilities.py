"""Canonical WorkBuddy connector capability negotiation contract.

Unknown but well-formed capabilities are reported as unsupported rather than
silently treated as negotiated. Malformed or duplicate declarations fail closed.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

SUPPORTED_CONNECTOR_CAPABILITIES = (
    "claim",
    "heartbeat",
    "progress",
    "renew",
    "result",
    "start",
)
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_CAPABILITIES = 20


def negotiate_capabilities(requested: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (negotiated, unsupported), both stable and sorted.

    The client may advertise capabilities added by a newer connector. Those
    names are reported to the client but never enabled by this server.
    """
    if isinstance(requested, (str, bytes)) or not isinstance(requested, Sequence):
        raise ValueError("capabilities must be a sequence of names")
    if len(requested) > MAX_CAPABILITIES:
        raise ValueError("too many connector capabilities")

    names: list[str] = []
    seen: set[str] = set()
    for name in requested:
        if not isinstance(name, str) or not _CAPABILITY_NAME.fullmatch(name):
            raise ValueError("invalid connector capability name")
        if name in seen:
            raise ValueError("duplicate connector capability")
        seen.add(name)
        names.append(name)

    supported = set(SUPPORTED_CONNECTOR_CAPABILITIES)
    negotiated = sorted(seen & supported)
    unsupported = sorted(seen - supported)
    return negotiated, unsupported
