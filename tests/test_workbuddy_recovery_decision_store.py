"""Persistence and compare-and-set tests for WorkBuddy recovery decisions."""
from __future__ import annotations

import sqlite3

import pytest
from concurrent.futures import ThreadPoolExecutor

from a2a_hub.workbuddy_presence import WorkBuddyPresenceStore


CONNECTOR = "workbuddy-personal"
SNAPSHOT = "a" * 64


def test_additive_event_table_migration_preserves_existing_error_state(tmp_path):
    path = tmp_path / "legacy-presence.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE workbuddy_connection_events (
                connector_id TEXT PRIMARY KEY,
                last_error_code TEXT,
                last_error_at REAL,
                reconnect_attempt INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            "INSERT INTO workbuddy_connection_events VALUES (?, ?, ?, ?)",
            (CONNECTOR, "timeout", 123.5, 3),
        )

    store = WorkBuddyPresenceStore(path)
    events = store.get_runtime_events(CONNECTOR)

    assert events["last_error_code"] == "timeout"
    assert events["last_error_at"] == 123.5
    assert events["reconnect_attempt"] == 3
    assert events["recovery_revision"] == 0
    assert events["decision_snapshot_id"] is None
    assert events["decision_disposition"] is None
    assert events["decision_at"] is None


def test_operator_decision_is_persisted_and_exact_retry_is_idempotent(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")

    accepted, recorded_at = store.record_recovery_decision(
        CONNECTOR, 0, SNAPSHOT, "acknowledged"
    )
    assert accepted is True
    events = store.get_runtime_events(CONNECTOR)
    assert events["decision_snapshot_id"] == SNAPSHOT
    assert events["decision_disposition"] == "acknowledged"
    assert events["decision_at"] == recorded_at

    replayed, replayed_at = store.record_recovery_decision(
        CONNECTOR, 0, SNAPSHOT, "acknowledged"
    )
    assert replayed is True
    assert replayed_at == recorded_at

    conflicting, _ = store.record_recovery_decision(
        CONNECTOR, 0, SNAPSHOT, "dismissed"
    )
    assert conflicting is False


def test_state_change_invalidates_previous_decision_and_stale_revision(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    assert store.record_recovery_decision(
        CONNECTOR, 0, SNAPSHOT, "deferred"
    )[0] is True

    store.record_error(CONNECTOR, "timeout")
    events = store.get_runtime_events(CONNECTOR)
    assert events["recovery_revision"] == 1
    assert events["decision_snapshot_id"] is None
    assert events["decision_disposition"] is None
    assert events["decision_at"] is None

    stale, _ = store.record_recovery_decision(
        CONNECTOR, 0, SNAPSHOT, "acknowledged"
    )
    assert stale is False


def test_handshake_and_accepted_heartbeat_advance_revision_and_clear_decision(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    store.record_handshake(CONNECTOR, "1.0", "test-client", ["heartbeat"])
    events = store.get_runtime_events(CONNECTOR)
    assert events["recovery_revision"] == 1

    assert store.record_recovery_decision(
        CONNECTOR, 1, SNAPSHOT, "acknowledged"
    )[0] is True
    assert store.record_heartbeat(
        CONNECTOR, "heartbeat-0001", "idle", None, heartbeat_seq=1
    ) is True

    events = store.get_runtime_events(CONNECTOR)
    assert events["recovery_revision"] == 2
    assert events["decision_snapshot_id"] is None
    assert events["decision_disposition"] is None


def test_concurrent_conflicting_decisions_allow_only_one_winner(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")

    def record(disposition: str):
        return store.record_recovery_decision(
            CONNECTOR, 0, SNAPSHOT, disposition
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, ["acknowledged", "dismissed"]))

    assert sorted(results) == [False, True]
    events = store.get_runtime_events(CONNECTOR)
    assert events["decision_snapshot_id"] == SNAPSHOT
    assert events["decision_disposition"] in {"acknowledged", "dismissed"}



def test_store_rejects_non_string_or_unknown_dispositions(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    for value in (None, [], "execute"):
        with pytest.raises(ValueError, match="recovery decision"):
            store.record_recovery_decision(CONNECTOR, 0, SNAPSHOT, value)
