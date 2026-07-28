from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"BASISHAWK-BACKUP\x00"
VERSION = b"\x01"
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + len(VERSION) + NONCE_SIZE
CHUNK_SIZE = 1024 * 1024
ARCHIVE_NAME_PATTERN = re.compile(
    r"^basis-hawk-\d{8}T\d{6}Z-(daily|weekly)\.bhbk$"
)
DAILY_ARCHIVE_NAME_PATTERN = re.compile(
    r"^basis-hawk-(\d{8}T\d{6}Z)-daily\.bhbk$"
)


class BackupError(RuntimeError):
    pass


def _backup_key() -> bytes:
    encoded = os.environ.get("BASIS_HAWK_BACKUP_KEY", "")
    try:
        value = base64.urlsafe_b64decode(encoded.encode())
    except (ValueError, TypeError) as exc:
        raise BackupError("BASIS_HAWK_BACKUP_KEY must be URL-safe base64") from exc
    if len(value) != 32:
        raise BackupError("BASIS_HAWK_BACKUP_KEY must decode to exactly 32 bytes")
    return value


def encrypt_stream(source: BinaryIO, target: BinaryIO, key: bytes) -> None:
    nonce = secrets.token_bytes(NONCE_SIZE)
    header = MAGIC + VERSION + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    target.write(header)
    while chunk := source.read(CHUNK_SIZE):
        target.write(encryptor.update(chunk))
    target.write(encryptor.finalize())
    target.write(encryptor.tag)


def decrypt_stream(source: BinaryIO, target: BinaryIO, key: bytes) -> None:
    header = source.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE or not header.startswith(MAGIC + VERSION):
        raise BackupError("unsupported backup format")
    source.seek(0, os.SEEK_END)
    size = source.tell()
    if size < HEADER_SIZE + TAG_SIZE:
        raise BackupError("truncated backup")
    source.seek(size - TAG_SIZE)
    tag = source.read(TAG_SIZE)
    nonce = header[-NONCE_SIZE:]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(header)
    source.seek(HEADER_SIZE)
    remaining = size - HEADER_SIZE - TAG_SIZE
    try:
        while remaining:
            chunk = source.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise BackupError("truncated backup")
            remaining -= len(chunk)
            target.write(decryptor.update(chunk))
        target.write(decryptor.finalize())
    except InvalidTag as exc:
        raise BackupError("backup authentication failed") from exc


def _database_environment() -> dict[str, str]:
    required = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise BackupError(f"missing PostgreSQL environment: {', '.join(missing)}")
    return {**os.environ}


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksum(path: Path) -> None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    checksum_path.write_text(f"{_checksum(path)}  {path.name}\n", encoding="ascii")
    checksum_path.chmod(0o600)


def _verify_checksum(path: Path) -> None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    if not checksum_path.is_file():
        raise BackupError("backup checksum file is missing")
    expected = checksum_path.read_text(encoding="ascii").split(maxsplit=1)[0]
    if not secrets.compare_digest(expected, _checksum(path)):
        raise BackupError("backup checksum mismatch")


def backup_status(directory: Path) -> dict[str, object]:
    if not directory.is_dir():
        return {
            "directory_available": False,
            "archive_count": 0,
            "latest": None,
            "archives": [],
        }
    archives: list[tuple[Path, os.stat_result]] = []
    for path in directory.glob("basis-hawk-*.bhbk"):
        if not ARCHIVE_NAME_PATTERN.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            archives.append((path, path.stat()))
        except FileNotFoundError:
            continue
    archives.sort(
        key=lambda item: (item[1].st_mtime, item[0].name),
        reverse=True,
    )
    if not archives:
        return {
            "directory_available": True,
            "archive_count": 0,
            "latest": None,
            "archives": [],
        }
    items = [
        {
            "name": path.name,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC),
            "checksum_present": path.with_suffix(path.suffix + ".sha256").is_file(),
            "latest": index == 0,
        }
        for index, (path, stat) in enumerate(archives)
    ]
    return {
        "directory_available": True,
        "archive_count": len(archives),
        "latest": {key: value for key, value in items[0].items() if key != "latest"},
        "archives": items,
    }


def delete_backups(directory: Path, archive_names: list[str]) -> list[str]:
    if not archive_names:
        raise BackupError("at least one backup archive is required")
    if len(archive_names) != len(set(archive_names)):
        raise BackupError("duplicate backup archive name")
    status = backup_status(directory)
    latest = status["latest"]
    targets: list[tuple[Path, Path]] = []
    for archive_name in archive_names:
        if not ARCHIVE_NAME_PATTERN.fullmatch(archive_name):
            raise BackupError("invalid backup archive name")
        if isinstance(latest, dict) and latest.get("name") == archive_name:
            raise BackupError("the latest backup cannot be deleted")
        path = directory / archive_name
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup archive does not exist")
        checksum_path = path.with_suffix(path.suffix + ".sha256")
        if checksum_path.is_symlink():
            raise BackupError("backup checksum path is invalid")
        targets.append((path, checksum_path))
    for path, checksum_path in targets:
        path.unlink()
        checksum_path.unlink(missing_ok=True)
    return archive_names


def delete_backup(directory: Path, archive_name: str) -> None:
    delete_backups(directory, [archive_name])


def _prune(directory: Path, suffix: str, keep: int) -> None:
    backups = sorted(directory.glob(f"basis-hawk-*-{suffix}.bhbk"), reverse=True)
    for path in backups[keep:]:
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)


def create_backup(
    *,
    directory: Path,
    now: datetime | None = None,
    daily_retention: int = 7,
    weekly_retention: int = 4,
) -> Path:
    key = _backup_key()
    env = _database_environment()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"basis-hawk-{timestamp:%Y%m%dT%H%M%SZ}-daily.bhbk"
    partial = path.with_suffix(path.suffix + ".partial")
    process = subprocess.Popen(
        [
            "pg_dump",
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            env["PGDATABASE"],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert process.stdout is not None
    try:
        with partial.open("xb") as target:
            encrypt_stream(process.stdout, target, key)
        if process.wait() != 0:
            raise BackupError(f"pg_dump failed with exit code {process.returncode}")
        partial.chmod(0o600)
        partial.replace(path)
        _write_checksum(path)
    except BaseException:
        process.kill()
        process.wait()
        partial.unlink(missing_ok=True)
        raise

    if timestamp.weekday() == 6:
        weekly = directory / path.name.replace("-daily.bhbk", "-weekly.bhbk")
        if not weekly.exists():
            os.link(path, weekly)
            _write_checksum(weekly)
    _prune(directory, "daily", daily_retention)
    _prune(directory, "weekly", weekly_retention)
    return path


def _pipe_decrypted(path: Path, command: list[str], env: dict[str, str]) -> None:
    key = _backup_key()
    _verify_checksum(path)
    with path.open("rb") as source, open(os.devnull, "wb") as sink:
        decrypt_stream(source, sink, key)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert process.stdin is not None
    completed = False
    try:
        try:
            with path.open("rb") as source:
                decrypt_stream(source, process.stdin, key)
        except BrokenPipeError:
            # pg_restore --list can finish successfully after reading only the
            # authenticated archive header and TOC. The complete archive was
            # already decrypted to the null sink above, including GCM finalization.
            pass
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        return_code = process.wait()
        completed = True
        if return_code != 0:
            raise BackupError(f"{command[0]} failed with exit code {process.returncode}")
    except BaseException:
        if not completed:
            process.kill()
            process.wait()
        raise
    finally:
        if not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass


def verify_backup(path: Path) -> None:
    _pipe_decrypted(path, ["pg_restore", "--list"], _database_environment())


def _database_has_user_tables(env: dict[str, str]) -> bool:
    result = subprocess.run(
        [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--dbname",
            env["PGDATABASE"],
            "--command",
            "SELECT count(*) FROM pg_catalog.pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema');",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    if result.returncode != 0:
        raise BackupError(f"psql preflight failed with exit code {result.returncode}")
    return int(result.stdout.strip()) > 0


def restore_backup(path: Path, *, confirmed: bool, clean: bool) -> None:
    if not confirmed:
        raise BackupError("restore requires --confirmed")
    env = _database_environment()
    if _database_has_user_tables(env) and not clean:
        raise BackupError(
            "target database is not empty; use a new database or explicitly add --clean"
        )
    command = [
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        "--dbname",
        env["PGDATABASE"],
    ]
    if clean:
        command.extend(["--clean", "--if-exists"])
    _pipe_decrypted(path, command, env)


def seconds_until_next_backup(
    directory: Path,
    interval_seconds: int,
    *,
    now: datetime | None = None,
) -> float:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    latest: datetime | None = None
    for path in directory.glob("basis-hawk-*-daily.bhbk"):
        match = DAILY_ARCHIVE_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        timestamp = datetime.strptime(
            match.group(1),
            "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=UTC)
        if latest is None or timestamp > latest:
            latest = timestamp
    if latest is None:
        return 0
    archive_age = max(0, (current - latest).total_seconds())
    return max(0, interval_seconds - archive_age)


def run_loop(directory: Path, interval_seconds: int) -> None:
    initial_delay = seconds_until_next_backup(directory, interval_seconds)
    if initial_delay > 0:
        print(
            f"backup: next archive in {int(initial_delay)} seconds",
            flush=True,
        )
        time.sleep(initial_delay)
    while True:
        try:
            path = create_backup(directory=directory)
            print(f"backup: created {path.name}", flush=True)
        except (BackupError, OSError) as exc:
            print(f"backup: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(prog="basis-hawk-backup")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(os.environ.get("BASIS_HAWK_BACKUP_DIRECTORY", "/backups")),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create")
    loop_parser = subparsers.add_parser("loop")
    loop_parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.environ.get("BASIS_HAWK_BACKUP_INTERVAL_SECONDS", "86400")),
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("path", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("path", type=Path)
    restore_parser.add_argument("--confirmed", action="store_true")
    restore_parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(create_backup(directory=args.directory))
        elif args.command == "loop":
            if args.interval_seconds < 3600:
                raise BackupError("backup interval must be at least 3600 seconds")
            run_loop(args.directory, args.interval_seconds)
        elif args.command == "verify":
            verify_backup(args.path)
            print(f"backup: verified {args.path.name}")
        else:
            restore_backup(args.path, confirmed=args.confirmed, clean=args.clean)
            print(f"backup: restored {args.path.name}")
    except (BackupError, OSError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
