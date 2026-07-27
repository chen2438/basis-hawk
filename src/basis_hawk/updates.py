from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import UUID

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
UPDATE_STATES = {
    "idle",
    "queued",
    "checking",
    "up_to_date",
    "update_available",
    "updating",
    "succeeded",
    "failed",
}
STATUS_KEYS = {
    "version",
    "state",
    "current_commit",
    "available_commit",
    "request_id",
    "checked_at",
    "completed_at",
    "error_code",
}


class UpdateError(RuntimeError):
    pass


def _parse_status(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise UpdateError("update status is unavailable")
    if path.stat().st_size > 4096:
        raise UpdateError("update status is invalid")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in STATUS_KEYS or key in values:
            raise UpdateError("update status is invalid")
        values[key] = value
    if values.get("version") != "1" or values.get("state") not in UPDATE_STATES:
        raise UpdateError("update status is invalid")
    for key in ("current_commit", "available_commit"):
        value = values.get(key, "")
        if value and not COMMIT_PATTERN.fullmatch(value):
            raise UpdateError("update status is invalid")
    request_id = values.get("request_id", "")
    if request_id:
        try:
            UUID(request_id)
        except ValueError as exc:
            raise UpdateError("update status is invalid") from exc
    return values


def update_status(request_directory: Path, status_file: Path) -> dict[str, object]:
    try:
        values = _parse_status(status_file)
    except (OSError, UnicodeError, UpdateError):
        return {
            "enabled": False,
            "state": "unavailable",
            "current_commit": None,
            "available_commit": None,
            "request_id": None,
            "checked_at": None,
            "completed_at": None,
            "error_code": "update_agent_unavailable",
        }
    enabled = request_directory.is_dir() and os.access(request_directory, os.W_OK)
    request_pending = (
        enabled
        and not (request_directory / "request").is_symlink()
        and (request_directory / "request").is_file()
    )
    return {
        "enabled": enabled,
        "state": (
            "queued"
            if request_pending and values["state"] not in {"checking", "updating"}
            else values["state"] if enabled else "unavailable"
        ),
        "current_commit": values.get("current_commit") or None,
        "available_commit": values.get("available_commit") or None,
        "request_id": values.get("request_id") or None,
        "checked_at": values.get("checked_at") or None,
        "completed_at": values.get("completed_at") or None,
        "error_code": (
            values.get("error_code") or None
            if enabled
            else "update_agent_unavailable"
        ),
    }


def enqueue_update(
    request_directory: Path,
    *,
    action: str,
    request_id: UUID,
    target_commit: str | None = None,
) -> None:
    if action not in {"check", "update"}:
        raise UpdateError("unsupported update action")
    target = target_commit or ""
    if action == "update" and not COMMIT_PATTERN.fullmatch(target):
        raise UpdateError("invalid update target")
    if action == "check" and target:
        raise UpdateError("update check cannot include a target")
    if (
        request_directory.is_symlink()
        or not request_directory.is_dir()
        or not os.access(request_directory, os.W_OK)
    ):
        raise UpdateError("update agent is unavailable")

    request_path = request_directory / "request"
    temporary_path = request_directory / f".request-{request_id}.tmp"
    payload = (
        "version=1\n"
        f"request_id={request_id}\n"
        f"action={action}\n"
        f"target_commit={target}\n"
    ).encode("ascii")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary_path, request_path, follow_symlinks=False)
    except FileExistsError as exc:
        raise UpdateError("another update operation is already queued") from exc
    except OSError as exc:
        raise UpdateError("could not queue the update operation") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
