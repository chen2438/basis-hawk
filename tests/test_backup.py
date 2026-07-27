from __future__ import annotations

import base64
import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from basis_hawk.backup import (
    BackupError,
    _pipe_decrypted,
    _prune,
    _write_checksum,
    backup_status,
    decrypt_stream,
    delete_backup,
    encrypt_stream,
)


def test_encrypted_backup_round_trip_and_authentication() -> None:
    key = os.urandom(32)
    encrypted = io.BytesIO()
    encrypt_stream(io.BytesIO(b"postgres archive contents" * 1000), encrypted, key)

    restored = io.BytesIO()
    encrypted.seek(0)
    decrypt_stream(encrypted, restored, key)
    assert restored.getvalue() == b"postgres archive contents" * 1000

    damaged = bytearray(encrypted.getvalue())
    damaged[-20] ^= 1
    with pytest.raises(BackupError, match="authentication failed"):
        decrypt_stream(io.BytesIO(damaged), io.BytesIO(), key)


def test_encrypted_backup_rejects_wrong_key() -> None:
    encrypted = io.BytesIO()
    encrypt_stream(io.BytesIO(b"archive"), encrypted, os.urandom(32))
    encrypted.seek(0)
    with pytest.raises(BackupError, match="authentication failed"):
        decrypt_stream(encrypted, io.BytesIO(), os.urandom(32))


def test_retention_keeps_newest_files_and_checksums(tmp_path: Path) -> None:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    for offset in range(10):
        stamp = now - timedelta(days=offset)
        backup = tmp_path / f"basis-hawk-{stamp:%Y%m%dT000000Z}-daily.bhbk"
        backup.write_bytes(b"archive")
        backup.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")

    _prune(tmp_path, "daily", 7)

    assert len(list(tmp_path.glob("*-daily.bhbk"))) == 7
    assert len(list(tmp_path.glob("*-daily.bhbk.sha256"))) == 7


def test_backup_status_reports_only_archive_metadata(tmp_path: Path) -> None:
    assert backup_status(tmp_path / "missing") == {
        "directory_available": False,
        "archive_count": 0,
        "latest": None,
        "archives": [],
    }
    older = tmp_path / "basis-hawk-20260725T000000Z-daily.bhbk"
    latest = tmp_path / "basis-hawk-20260726T000000Z-daily.bhbk"
    older.write_bytes(b"old")
    latest.write_bytes(b"new archive")
    latest.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")
    os.utime(older, (1, 1))
    os.utime(latest, (2, 2))

    value = backup_status(tmp_path)

    assert value["directory_available"] is True
    assert value["archive_count"] == 2
    assert value["latest"] == {
        "name": latest.name,
        "size_bytes": 11,
        "modified_at": datetime.fromtimestamp(2, UTC),
        "checksum_present": True,
    }
    assert [item["name"] for item in value["archives"]] == [latest.name, older.name]
    assert value["archives"][0]["latest"] is True
    assert value["archives"][1]["latest"] is False


def test_manual_backup_deletion_rejects_latest_and_removes_old_pair(
    tmp_path: Path,
) -> None:
    older = tmp_path / "basis-hawk-20260725T000000Z-daily.bhbk"
    latest = tmp_path / "basis-hawk-20260726T000000Z-daily.bhbk"
    for index, path in enumerate((older, latest), start=1):
        path.write_bytes(b"archive")
        path.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")
        os.utime(path, (index, index))

    with pytest.raises(BackupError, match="latest backup"):
        delete_backup(tmp_path, latest.name)
    with pytest.raises(BackupError, match="invalid backup archive name"):
        delete_backup(tmp_path, "../latest.bhbk")

    delete_backup(tmp_path, older.name)
    assert not older.exists()
    assert not older.with_suffix(".bhbk.sha256").exists()


def test_backup_key_must_be_independent_valid_material(monkeypatch: pytest.MonkeyPatch) -> None:
    from basis_hawk.backup import _backup_key

    monkeypatch.setenv("BASIS_HAWK_BACKUP_KEY", base64.urlsafe_b64encode(b"x" * 32).decode())
    assert _backup_key() == b"x" * 32
    monkeypatch.setenv("BASIS_HAWK_BACKUP_KEY", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(BackupError, match="exactly 32 bytes"):
        _backup_key()


def test_restore_authenticates_before_starting_database_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = os.urandom(32)
    monkeypatch.setenv("BASIS_HAWK_BACKUP_KEY", base64.urlsafe_b64encode(key).decode())
    path = tmp_path / "damaged.bhbk"
    encrypted = io.BytesIO()
    encrypt_stream(io.BytesIO(b"archive"), encrypted, key)
    damaged = bytearray(encrypted.getvalue())
    damaged[-20] ^= 1
    path.write_bytes(damaged)
    _write_checksum(path)

    def unexpected_process(*args: object, **kwargs: object) -> None:
        raise AssertionError("database command started before authentication")

    monkeypatch.setattr("basis_hawk.backup.subprocess.Popen", unexpected_process)
    with pytest.raises(BackupError, match="authentication failed"):
        _pipe_decrypted(path, ["pg_restore"], os.environ.copy())


@pytest.mark.parametrize("return_code", [0, 7])
def test_pipe_accepts_early_close_only_when_command_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
) -> None:
    key = os.urandom(32)
    monkeypatch.setenv(
        "BASIS_HAWK_BACKUP_KEY",
        base64.urlsafe_b64encode(key).decode(),
    )
    path = tmp_path / "valid.bhbk"
    encrypted = io.BytesIO()
    encrypt_stream(io.BytesIO(b"archive payload"), encrypted, key)
    path.write_bytes(encrypted.getvalue())
    _write_checksum(path)

    class ClosedInput(io.BytesIO):
        def write(self, value: bytes) -> int:
            raise BrokenPipeError

    class EarlyExitProcess:
        def __init__(self) -> None:
            self.stdin = ClosedInput()
            self.returncode: int | None = None
            self.killed = False

        def wait(self) -> int:
            self.returncode = return_code
            return return_code

        def kill(self) -> None:
            self.killed = True

    process = EarlyExitProcess()
    monkeypatch.setattr(
        "basis_hawk.backup.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    if return_code == 0:
        _pipe_decrypted(path, ["pg_restore", "--list"], os.environ.copy())
        assert process.killed is False
    else:
        with pytest.raises(BackupError, match="exit code 7"):
            _pipe_decrypted(
                path,
                ["pg_restore", "--list"],
                os.environ.copy(),
            )
        assert process.killed is False
