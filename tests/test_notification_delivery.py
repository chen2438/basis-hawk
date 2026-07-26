from __future__ import annotations

from datetime import UTC, datetime

import httpx

from basis_hawk.notifications import (
    NotificationDeliveryError,
    NotificationDeliveryService,
    SmtpSender,
    TelegramSender,
)
from basis_hawk.storage import Database, NotificationOutboxItem


class FakeSender:
    def __init__(self, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.sent: list[str] = []

    async def send(self, item: NotificationOutboxItem) -> None:
        self.sent.append(item.id)
        if self.error_code is not None:
            raise NotificationDeliveryError(self.error_code)


async def test_delivery_service_settles_channels_independently() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.enqueue_notification(
        dedupe_key="trade:filled:123",
        event_type="trade.filled",
        severity="info",
        channels={"telegram", "email"},
        subject="Filled",
        body="The paired trade filled.",
    )
    telegram = FakeSender()
    email = FakeSender("remote_unavailable")
    service = NotificationDeliveryService(
        database,
        {"telegram": telegram, "email": email},
    )

    assert await service.run_once() == 2
    rows = await database.notification_outbox()
    status_by_channel = {item.channel: item.status for item in rows}
    assert status_by_channel == {"telegram": "sent", "email": "retry"}
    assert len(telegram.sent) == len(email.sent) == 1
    await database.close()


async def test_delivery_service_sanitizes_unexpected_failure() -> None:
    class UnsafeSender:
        async def send(self, item: NotificationOutboxItem) -> None:
            raise RuntimeError("token=should-never-be-persisted")

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.enqueue_notification(
        dedupe_key="risk:test",
        event_type="risk.test",
        severity="critical",
        channels={"telegram"},
        subject="Test",
        body="Test notification.",
    )
    service = NotificationDeliveryService(database, {"telegram": UnsafeSender()})

    await service.run_once()
    [row] = await database.notification_outbox()
    assert row.status == "retry"
    assert row.last_error_code == "internal_error"
    await database.close()


async def test_telegram_sender_posts_protected_plain_text() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = TelegramSender(
        bot_token="123456:valid-token",
        chat_id="-100123456",
        timeout_seconds=1,
        client=client,
    )
    item = NotificationOutboxItem(
        id="id",
        dedupe_key="key",
        event_type="test",
        severity="info",
        channel="telegram",
        subject="Basis Hawk",
        body="Trade filled.",
        status="sending",
        attempts=1,
        next_attempt_at=datetime.now(UTC),
        last_error_code=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        sent_at=None,
    )

    await sender.send(item)
    assert captured["path"] == "/bot123456:valid-token/sendMessage"
    assert b'"protect_content":true' in captured["payload"]
    assert b"Basis Hawk\\n\\nTrade filled." in captured["payload"]
    await client.aclose()


async def test_smtp_sender_uses_starttls_and_authentication(monkeypatch) -> None:
    calls: list[object] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            calls.append(("connect", host, port, timeout))

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_args: object) -> None:
            calls.append("close")

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, *, context: object) -> None:
            calls.append(("starttls", context is not None))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(
            self,
            message: object,
            *,
            from_addr: str,
            to_addrs: list[str],
        ) -> None:
            calls.append(("send", from_addr, to_addrs, message["Subject"]))

    monkeypatch.setattr("basis_hawk.notifications.smtplib.SMTP", FakeSmtp)
    sender = SmtpSender(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="hawk",
        password="secret",
        sender="hawk@example.com",
        recipients=("owner@example.com",),
        timeout_seconds=3,
    )
    item = NotificationOutboxItem(
        id="id",
        dedupe_key="key",
        event_type="test",
        severity="critical",
        channel="email",
        subject="Critical alert",
        body="Execution paused.",
        status="sending",
        attempts=1,
        next_attempt_at=datetime.now(UTC),
        last_error_code=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        sent_at=None,
    )

    await sender.send(item)
    assert calls[0] == ("connect", "smtp.example.com", 587, 3)
    assert calls[1:4] == ["ehlo", ("starttls", True), "ehlo"]
    assert calls[4] == ("login", "hawk", "secret")
    assert calls[5] == (
        "send",
        "hawk@example.com",
        ["owner@example.com"],
        "Critical alert",
    )
