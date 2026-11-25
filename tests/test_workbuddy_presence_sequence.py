"""T78-T84 WorkBuddy server presence heartbeat sequence guard tests."""
from __future__ import annotations

import sqlite3

import pytest

from a2a_hub.workbuddy_presence import WorkBuddyPresenceStore


def test_presence_store_migrates_legacy_schema_additively(tmp_path):
    path = tmp_path / "legacy-presence.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE workbuddy_presence (
            connector_id TEXT PRIMARY KEY, protocol_version TEXT NOT NULL,
            client_version TEXT NOT NULL, capabilities_json TEXT NOT NULL,
            status TEXT NOT NULL, current_task_id TEXT, last_heartbeat_id TEXT,
            first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL
        )""")
        db.execute("""INSERT INTO workbuddy_presence VALUES
            ('workbuddy-a', '1.0', 'legacy', '[]', 'idle', NULL, NULL, 1, 1)""")
    store = WorkBuddyPresenceStore(path)
    state = store.get_presence("workbuddy-a", now=1)
    assert state["last_heartbeat_seq"] is None
    assert state["client_version"] == "legacy"
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(workbuddy_presence)")}
    assert "last_heartbeat_seq" in columns


def test_heartbeat_sequence_is_monotonic_and_replay_is_rejected(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    store.record_handshake("workbuddy-a", "1.0", "client", ["heartbeat"])
    assert store.record_heartbeat("workbuddy-a", "heartbeat-0001", "idle", None, 1)
    assert store.record_heartbeat("workbuddy-a", "heartbeat-0002", "busy", "task-1", 2)
    assert not store.record_heartbeat("workbuddy-a", "heartbeat-old-01", "idle", None, 1)
    assert not store.record_heartbeat("workbuddy-a", "heartbeat-dup-01", "idle", None, 2)
    state = store.get_presence("workbuddy-a")
    assert state["last_heartbeat_seq"] == 2
    assert state["last_heartbeat_id"] == "heartbeat-0002"
    assert state["status"] == "busy"


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "2"])
def test_invalid_heartbeat_sequence_is_rejected(tmp_path, invalid):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    store.record_handshake("workbuddy-a", "1.0", "client", [])
    with pytest.raises(ValueError, match="positive integer"):
        store.record_heartbeat("workbuddy-a", "heartbeat-0001", "idle", None, invalid)


def test_new_handshake_resets_sequence_for_new_connection_epoch(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    store.record_handshake("workbuddy-a", "1.0", "client", [])
    assert store.record_heartbeat("workbuddy-a", "heartbeat-0001", "idle", None, 12)
    store.record_handshake("workbuddy-a", "1.0", "client", [])
    assert store.get_presence("workbuddy-a")["last_heartbeat_seq"] is None
    assert store.record_heartbeat("workbuddy-a", "heartbeat-0001", "idle", None, 1)


def test_legacy_unsequenced_heartbeat_remains_supported(tmp_path):
    store = WorkBuddyPresenceStore(tmp_path / "presence.db")
    store.record_handshake("workbuddy-a", "1.0", "legacy", [])
    assert store.record_heartbeat("workbuddy-a", "heartbeat-legacy-01", "idle", None)
    assert store.get_presence("workbuddy-a")["last_heartbeat_seq"] is None
