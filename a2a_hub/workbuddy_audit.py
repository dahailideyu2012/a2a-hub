"""Structured, redacted audit events for the WorkBuddy connector."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Literal

AUDIT = logging.getLogger("a2a_hub.audit.workbuddy")

_EVENTS = {
    "claim", "start", "renew", "progress", "result", "handshake",
    "heartbeat", "presence", "diagnostics", "callback_status", "callback_retry",
    "connection_error", "reconnect_attempt",
}
_OUTCOMES = {
    "ok", "denied", "unauthenticated", "rate_limited",
    "conflict", "unavailable", "error",
}
_REASONS = {
    "token_missing", "token_mismatch", "connector_identity_mismatch",
    "conflict", "rate_limited", "unavailable", "http_error",
    "internal_error",
}
_CONNECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_FP = re.compile(r"^[0-9a-f]{8}$")


def fingerprint(token: str | None) -> str:
    """Return only an eight-hex SHA-256 prefix; never return the supplied token."""
    if not token:
        return "-"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def emit(
    event: Literal[
        "claim", "start", "renew", "progress", "result", "handshake",
        "heartbeat", "presence", "diagnostics", "callback_status", "callback_retry",
        "connection_error", "reconnect_attempt",
    ],
    outcome: Literal[
        "ok", "denied", "unauthenticated", "rate_limited",
        "conflict", "unavailable", "error",
    ],
    *,
    connector_id: str | None,
    status: int,
    token_fp: str,
    duration_ms: int = 0,
    reason: str | None = None,
) -> None:
    """Emit a single-line JSON record with a closed, non-sensitive field set."""
    if event not in _EVENTS or outcome not in _OUTCOMES:
        raise ValueError("unsupported WorkBuddy audit event")
    if token_fp != "-" and not _TOKEN_FP.fullmatch(token_fp):
        raise ValueError("invalid token fingerprint")
    if reason is not None and reason not in _REASONS:
        raise ValueError("unsupported WorkBuddy audit reason")

    safe_connector_id = (
        connector_id
        if isinstance(connector_id, str) and _CONNECTOR_ID.fullmatch(connector_id)
        else None
    )
    record = {
        "event": event,
        "outcome": outcome,
        "connector_id": safe_connector_id,
        "status": int(status),
        "token_fp": token_fp,
        "duration_ms": max(0, int(duration_ms)),
    }
    if reason is not None:
        record["reason"] = reason
    AUDIT.info(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
