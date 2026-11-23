from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.workbuddy_sandbox import (
    DEFAULT_PORT,
    create_env_file,
    load_env_file,
    main,
    validate_port,
)


def test_init_creates_three_distinct_credentials_without_printing_them(tmp_path: Path, capsys):
    target = tmp_path / ".env.workbuddy-sandbox"
    create_env_file(target)
    output = capsys.readouterr().out
    values = load_env_file(target)
    assert values["A2A_WORKBUDDY_SANDBOX_PORT"] == str(DEFAULT_PORT)
    credentials = [
        values["A2A_API_TOKEN"],
        values["A2A_CHATGPT_INGRESS_TOKEN"],
        values["A2A_WORKBUDDY_CONNECTOR_TOKEN"],
    ]
    assert len(set(credentials)) == 3
    assert all(len(value) >= 32 for value in credentials)
    assert all(secret not in output for secret in credentials)
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_init_refuses_to_overwrite_existing_env_file(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text("keep-this-file", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_env_file(target)
    assert target.read_text(encoding="utf-8") == "keep-this-file"


@pytest.mark.parametrize("port", [0, 1023, 65536, -1])
def test_invalid_ports_are_rejected(port: int):
    with pytest.raises(ValueError):
        validate_port(port)


@pytest.mark.parametrize("port", [1024, 18081, 65535])
def test_valid_ports_are_accepted(port: int):
    assert validate_port(port) == port


def test_env_reader_rejects_reused_credentials(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text(
        "A2A_WORKBUDDY_SANDBOX_PORT=18081\\n"
        "A2A_API_TOKEN=" + "a" * 48 + "\\n"
        "A2A_CHATGPT_INGRESS_TOKEN=" + "a" * 48 + "\\n"
        "A2A_WORKBUDDY_CONNECTOR_TOKEN=" + "c" * 48 + "\\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid or reused"):
        load_env_file(target)


def test_env_reader_rejects_duplicate_keys(tmp_path: Path):
    target = tmp_path / ".env.workbuddy-sandbox"
    target.write_text(
        "A2A_WORKBUDDY_SANDBOX_PORT=18081\\n"
        "A2A_WORKBUDDY_SANDBOX_PORT=18082\\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_env_file(target)


def test_destroy_refuses_without_explicit_confirmation(monkeypatch, capsys):
    compose_calls = []
    monkeypatch.setattr(
        "scripts.workbuddy_sandbox._compose",
        lambda *args: compose_calls.append(args) or 0,
    )

    assert main(["destroy"]) == 2
    assert compose_calls == []
    assert "--confirm-delete-data" in capsys.readouterr().err


def test_destroy_removes_volume_only_after_explicit_confirmation(monkeypatch):
    compose_calls = []
    monkeypatch.setattr(
        "scripts.workbuddy_sandbox._compose",
        lambda *args: compose_calls.append(args) or 0,
    )

    assert main(["destroy", "--confirm-delete-data"]) == 0
    assert compose_calls == [("down", "--volumes")]
