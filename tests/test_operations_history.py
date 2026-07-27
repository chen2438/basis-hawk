from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
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
    old_backup = tmp_path / "basis-hawk-20260726T000000Z-daily.bhbk"
    backup = tmp_path / "basis-hawk-20260727T000000Z-daily.bhbk"
    for index, path in enumerate((old_backup, backup), start=1):
        path.write_bytes(b"encrypted")
        path.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")
        path.chmod(0o600)
        path_time = datetime(2026, 7, 25 + index, tzinfo=UTC).timestamp()
        os.utime(path, (path_time, path_time))
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
            assert status.json()["archive_count"] == 2
            assert status.json()["latest"] == {
                "name": backup.name,
                "size_bytes": 9,
                "modified_at": status.json()["latest"]["modified_at"],
                "checksum_present": True,
            }
            assert [item["name"] for item in status.json()["archives"]] == [
                backup.name,
                old_backup.name,
            ]

            delete_latest = await client.request(
                "DELETE",
                f"/api/operations/backups/{backup.name}",
                json={"confirmed": True},
            )
            assert delete_latest.status_code == 409
            deleted = await client.request(
                "DELETE",
                f"/api/operations/backups/{old_backup.name}",
                json={"confirmed": True},
            )
            assert deleted.status_code == 200
            assert not old_backup.exists()

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

            old = datetime.now(UTC) - timedelta(days=60)
            [old_notification] = await database.enqueue_notification(
                dedupe_key="old:sent",
                event_type="old.sent",
                severity="info",
                channels={"telegram"},
                subject="Old sent notification",
                body="safe body",
                now=old,
            )
            await database.claim_notifications(now=old)
            await database.mark_notification_sent(old_notification.id, now=old)
            await database.enqueue_notification(
                dedupe_key="old:pending",
                event_type="old.pending",
                severity="info",
                channels={"email"},
                subject="Old pending notification",
                body="safe body",
                now=old,
            )
            pruned = await client.post(
                "/api/operations/logs/prune",
                json={"retention_days": 30, "confirmed": True},
            )
            assert pruned.status_code == 200
            assert pruned.json()["deleted_count"] == 1
            remaining = await database.notification_outbox()
            assert {item.event_type for item in remaining} == {
                "notification.test",
                "old.pending",
            }
    finally:
        await database.close()
        get_config.cache_clear()


async def test_batch_backup_deletion_preserves_latest_and_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_names = [
        "basis-hawk-20260725T000000Z-daily.bhbk",
        "basis-hawk-20260726T000000Z-daily.bhbk",
        "basis-hawk-20260727T000000Z-daily.bhbk",
    ]
    paths = [tmp_path / name for name in archive_names]
    for index, path in enumerate(paths, start=1):
        path.write_bytes(b"encrypted")
        path.with_suffix(".bhbk.sha256").write_text("checksum", encoding="ascii")
        os.utime(path, (index, index))
    monkeypatch.setenv("BASIS_HAWK_BACKUP_DIRECTORY", str(tmp_path))
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
            rejected = await client.post(
                "/api/operations/backups/batch-delete",
                json={
                    "archive_names": [archive_names[0], archive_names[2]],
                    "confirmed": True,
                },
            )
            assert rejected.status_code == 409
            assert all(path.exists() for path in paths)

            deleted = await client.post(
                "/api/operations/backups/batch-delete",
                json={
                    "archive_names": archive_names[:2],
                    "confirmed": True,
                },
            )
            assert deleted.status_code == 200
            assert deleted.json() == {
                "deleted_count": 2,
                "archive_names": archive_names[:2],
            }
            assert not paths[0].exists()
            assert not paths[1].exists()
            assert paths[2].exists()

            audit = await client.get(
                "/api/operations/audit",
                params={"event_type": "backup.batch_deleted"},
            )
            [event] = audit.json()["items"]
            assert event["details"] == {
                "archive_count": 2,
                "archive_names": archive_names[:2],
            }
    finally:
        await database.close()
        get_config.cache_clear()
