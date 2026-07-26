from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from basis_hawk.api import create_app
from basis_hawk.config import get_config
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


async def test_operations_history_is_filtered_paginated_and_redacted() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    await database.append_audit(
        "credentials.updated",
        actor="admin",
        details={
            "exchange": "binance",
            "api_key": "must-not-leak",
            "nested": {"password": "must-not-leak"},
        },
    )
    await database.append_audit(
        "execution.paused",
        actor="admin",
        details={"reason": "maintenance"},
    )
    await database.enqueue_notification(
        dedupe_key="execution:paused:1",
        event_type="execution.paused",
        severity="critical",
        channels={"telegram", "email"},
        subject="Execution paused",
        body="private delivery body",
    )
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        audit = await client.get(
            "/api/operations/audit",
            params={"event_type": "credentials.updated", "limit": 1},
        )
        assert audit.status_code == 200
        assert audit.json()["offset"] == 0
        assert audit.json()["items"] == [
            {
                "id": audit.json()["items"][0]["id"],
                "occurred_at": audit.json()["items"][0]["occurred_at"],
                "event_type": "credentials.updated",
                "actor": "admin",
                "details": {
                    "exchange": "binance",
                    "api_key": "[redacted]",
                    "nested": {"password": "[redacted]"},
                },
            }
        ]

        notifications = await client.get(
            "/api/operations/notifications",
            params={"channel": "email", "status": "pending"},
        )
        assert notifications.status_code == 200
        [item] = notifications.json()["items"]
        assert item["channel"] == "email"
        assert item["status"] == "pending"
        assert item["subject"] == "Execution paused"
        assert "body" not in item
        assert "dedupe_key" not in item
        assert "private delivery body" not in notifications.text
    await database.close()


async def test_notification_test_and_backup_status_are_operator_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "basis-hawk-20260727T000000Z-daily.bhbk"
    backup.write_bytes(b"encrypted")
    backup.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")
    monkeypatch.setenv("BASIS_HAWK_BACKUP_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("BASIS_HAWK_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("BASIS_HAWK_TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("BASIS_HAWK_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("BASIS_HAWK_SMTP_FROM", "hawk@example.test")
    monkeypatch.setenv("BASIS_HAWK_SMTP_TO", "admin@example.test")
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
            status = await client.get("/api/operations/backup")
            assert status.status_code == 200
            assert status.json()["archive_count"] == 1
            assert status.json()["latest"] == {
                "name": backup.name,
                "size_bytes": 9,
                "modified_at": status.json()["latest"]["modified_at"],
                "checksum_present": True,
            }

            queued = await client.post(
                "/api/operations/notifications/test",
                json={"channels": ["telegram", "email"], "confirmed": True},
            )
            assert queued.status_code == 200
            assert {item["channel"] for item in queued.json()["items"]} == {
                "telegram",
                "email",
            }
            assert {item["status"] for item in queued.json()["items"]} == {
                "pending"
            }
            history = await client.get("/api/operations/notifications")
            assert {
                item["event_type"] for item in history.json()["items"]
            } == {"notification.test"}
            assert "body" not in history.text
            audit = await client.get(
                "/api/operations/audit",
                params={"event_type": "notification.test_requested"},
            )
            assert len(audit.json()["items"]) == 1
    finally:
        await database.close()
        get_config.cache_clear()
