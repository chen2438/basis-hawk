import httpx
from pydantic import SecretStr

from basis_hawk.api import create_app
from basis_hawk.config import AppConfig
from basis_hawk.notifications import TelegramCommandService
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


async def test_telegram_commands_are_whitelisted_read_only_and_deduplicated() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.set_execution_control(state="ready", reason="healthy")
    commands = TelegramCommandService(database, allowed_chat_id="-100123")

    assert not await commands.handle_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": -999},
                "text": "/status",
            },
        }
    )
    assert await database.notification_outbox() == []

    update = {
        "update_id": 2,
        "message": {
            "chat": {"id": -100123},
            "text": "/status@basis_hawk_bot ignored",
        },
    }
    assert await commands.handle_update(update)
    assert await commands.handle_update(update)
    [response] = await database.notification_outbox()
    assert response.channel == "telegram"
    assert response.event_type == "telegram.command_response"
    assert "Execution: ready" in response.body
    assert "Automation: disabled" in response.body

    assert await commands.handle_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -100123},
                "text": "/pause",
            },
        }
    )
    rows = await database.notification_outbox()
    unknown = next(item for item in rows if item.dedupe_key.endswith(":3"))
    assert unknown.body == "Read-only commands: /status /positions /alerts /health"
    assert (await database.execution_control()).state == "ready"
    await database.close()


async def test_telegram_webhook_requires_secret_without_admin_session(
    monkeypatch,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    config = AppConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        credential_master_key=None,
        telegram_chat_id="-100123",
        telegram_webhook_secret=SecretStr("valid_webhook_secret"),
    )
    monkeypatch.setattr("basis_hawk.api.get_config", lambda: config)
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=True,
    )
    update = {
        "update_id": 100,
        "message": {
            "chat": {"id": -100123},
            "text": "/health",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.post(
            "/api/integrations/telegram/webhook",
            json=update,
        )
        assert missing.status_code == 403
        accepted = await client.post(
            "/api/integrations/telegram/webhook",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "valid_webhook_secret"
            },
            json=update,
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True}
    [response] = await database.notification_outbox()
    assert response.dedupe_key == "telegram:update:100"
    await service.stop()
