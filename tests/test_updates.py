from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from basis_hawk.api import create_app
from basis_hawk.config import get_config
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database
from basis_hawk.updates import UpdateError, enqueue_update, update_status

CURRENT = "1" * 40
AVAILABLE = "2" * 40


def write_status(
    path: Path,
    *,
    state: str = "update_available",
    current: str = CURRENT,
    available: str = AVAILABLE,
) -> None:
    path.write_text(
        "\n".join(
            [
                "version=1",
                f"state={state}",
                f"current_commit={current}",
                f"available_commit={available}",
                "request_id=",
                "checked_at=2026-07-27T12:00:00Z",
                "completed_at=",
                "error_code=",
                "",
            ]
        ),
        encoding="ascii",
    )


def test_update_status_and_atomic_request_queue(tmp_path: Path) -> None:
    request_directory = tmp_path / "request"
    request_directory.mkdir()
    status_file = tmp_path / "status"
    write_status(status_file)

    status = update_status(request_directory, status_file)
    assert status["enabled"] is True
    assert status["state"] == "update_available"
    assert status["available_commit"] == AVAILABLE

    request_id = uuid4()
    enqueue_update(
        request_directory,
        action="update",
        request_id=request_id,
        target_commit=AVAILABLE,
    )
    assert (request_directory / "request").read_text(encoding="ascii") == (
        "version=1\n"
        f"request_id={request_id}\n"
        "action=update\n"
        f"target_commit={AVAILABLE}\n"
    )
    with pytest.raises(UpdateError, match="already queued"):
        enqueue_update(
            request_directory,
            action="check",
            request_id=uuid4(),
        )


def test_update_status_rejects_untrusted_metadata(tmp_path: Path) -> None:
    request_directory = tmp_path / "request"
    request_directory.mkdir()
    status_file = tmp_path / "status"
    status_file.write_text(
        "version=1\nstate=update_available\ncurrent_commit=../../etc/passwd\n",
        encoding="ascii",
    )
    status = update_status(request_directory, status_file)
    assert status["enabled"] is False
    assert status["error_code"] == "update_agent_unavailable"


async def test_update_api_checks_and_pauses_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_directory = tmp_path / "request"
    request_directory.mkdir()
    status_directory = tmp_path / "status"
    status_directory.mkdir()
    status_file = status_directory / "status"
    write_status(status_file)
    monkeypatch.setenv(
        "BASIS_HAWK_UPDATE_REQUEST_DIRECTORY",
        str(request_directory),
    )
    monkeypatch.setenv("BASIS_HAWK_UPDATE_STATUS_FILE", str(status_file))
    get_config.cache_clear()
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    try:
        app = create_app(service, manage_lifecycle=False, auth_required=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            status = await client.get("/api/operations/update")
            assert status.json()["state"] == "update_available"

            checked = await client.post("/api/operations/update/check", json={})
            assert checked.status_code == 200
            UUID(checked.json()["request_id"])
            assert "action=check" in (
                request_directory / "request"
            ).read_text(encoding="ascii")

            (request_directory / "request").unlink()
            applied = await client.post(
                "/api/operations/update/apply",
                json={"target_commit": AVAILABLE, "confirmed": True},
            )
            assert applied.status_code == 200
            assert "action=update" in (
                request_directory / "request"
            ).read_text(encoding="ascii")
            control = await database.execution_control()
            assert control is not None
            assert control.state == "paused"
            assert control.reason == "software update requested"
            events = await database.audit_events(limit=10)
            assert {event.event_type for event in events} >= {
                "software.update_check_requested",
                "software.update_requested",
            }
    finally:
        await database.close()
        get_config.cache_clear()
