from __future__ import annotations

import sqlite3

import pytest

from scripts.sqlite_maintenance import (
    MaintenanceError,
    backup_database,
    restore_database,
    verify_database,
)


def create_database(path, value="before"):
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def read_values(path):
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT value FROM sample")]
    finally:
        connection.close()


def test_backup_includes_committed_wal_data_and_verifies(tmp_path):
    database = tmp_path / "hub.db"
    backup = tmp_path / "backups" / "hub.db"
    create_database(database, "committed-in-wal")

    result = backup_database(database, backup)

    assert result == backup.resolve()
    assert verify_database(backup) == backup.resolve()
    assert read_values(backup) == ["committed-in-wal"]


def test_restore_requires_explicit_overwrite_and_restores_data(tmp_path):
    backup = tmp_path / "backup.db"
    target = tmp_path / "hub.db"
    create_database(backup, "backup-value")
    create_database(target, "existing-value")

    with pytest.raises(MaintenanceError, match="destination exists"):
        restore_database(backup, target)

    restore_database(backup, target, overwrite=True)
    assert read_values(target) == ["backup-value"]
    assert verify_database(target) == target.resolve()


def test_backup_refuses_same_source_and_destination(tmp_path):
    database = tmp_path / "hub.db"
    create_database(database)

    with pytest.raises(MaintenanceError, match="must differ"):
        backup_database(database, database)


def test_restore_refuses_same_source_and_destination(tmp_path):
    database = tmp_path / "hub.db"
    create_database(database)

    with pytest.raises(MaintenanceError, match="must differ"):
        restore_database(database, database, overwrite=True)


def test_verify_rejects_missing_database(tmp_path):
    with pytest.raises(MaintenanceError, match="does not exist"):
        verify_database(tmp_path / "missing.db")
