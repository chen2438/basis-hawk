from __future__ import annotations

import httpx

from basis_hawk.api import create_app
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
