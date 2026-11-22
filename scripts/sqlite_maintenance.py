"""Safe SQLite backup, verification, and offline restore helpers.

Backups use SQLite's online backup API so committed WAL transactions are included.
Restore is an offline operation: stop every process using the destination database.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


class MaintenanceError(RuntimeError):
    """Raised when a database maintenance operation is unsafe or invalid."""


def _database_file(path: str | Path, *, must_exist: bool = True) -> Path:
    original = Path(path).expanduser()
    if original.is_symlink():
        raise MaintenanceError(f"database path must not be a symlink: {original}")
    target = original.resolve()
    if must_exist and not target.is_file():
        raise MaintenanceError(f"database file does not exist or is not a regular file: {target}")
    return target


def _integrity_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        details = "; ".join(str(row[0]) for row in rows[:5])
        raise MaintenanceError(f"SQLite integrity_check failed: {details}")


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.backup(destination)
        _integrity_check(destination)
    finally:
        destination.close()
        source.close()


def backup_database(database: str | Path, output: str | Path) -> Path:
    source = _database_file(database)
    raw_destination = Path(output).expanduser()
    if raw_destination.is_symlink():
        raise MaintenanceError("backup destination must not be a symlink")
    destination = raw_destination.resolve()
    if source == destination:
        raise MaintenanceError("backup destination must differ from source database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_dir():
        raise MaintenanceError("backup destination must be a file path")

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    temporary = Path(temp_name)
    try:
        _copy_database(source, temporary)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_database(database: str | Path) -> Path:
    source = _database_file(database)
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        _integrity_check(connection)
    finally:
        connection.close()
    return source


def restore_database(
    backup: str | Path, database: str | Path, *, overwrite: bool = False
) -> Path:
    source = verify_database(backup)
    raw_destination = Path(database).expanduser()
    if raw_destination.is_symlink():
        raise MaintenanceError("restore destination must not be a symlink")
    destination = raw_destination.resolve()
    if source == destination:
        raise MaintenanceError("restore source and destination must differ")
    if destination.exists() and not overwrite:
        raise MaintenanceError("destination exists; pass --overwrite after stopping the Hub")
    destination.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".restore.tmp", dir=str(destination.parent)
    )
    os.close(fd)
    temporary = Path(temp_name)
    try:
        _copy_database(source, temporary)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2A Hub SQLite maintenance")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("--database", required=True, help="source SQLite database")
    backup.add_argument("--output", required=True, help="backup file path")

    verify = commands.add_parser("verify", help="run SQLite integrity_check")
    verify.add_argument("--database", required=True, help="SQLite database or backup")

    restore = commands.add_parser("restore", help="restore a verified backup (Hub must be stopped)")
    restore.add_argument("--backup", required=True, help="verified backup file")
    restore.add_argument("--database", required=True, help="destination SQLite database")
    restore.add_argument(
        "--overwrite", action="store_true",
        help="replace an existing destination; only use after stopping the Hub",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(args.database, args.output)
        elif args.command == "verify":
            result = verify_database(args.database)
        else:
            result = restore_database(args.backup, args.database, overwrite=args.overwrite)
        print(f"{args.command} OK: {result}")
        return 0
    except (MaintenanceError, OSError, sqlite3.Error) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
