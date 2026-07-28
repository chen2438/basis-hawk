from decimal import Decimal

import httpx

from basis_hawk.api import create_app
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


def strategy_config() -> dict[str, object]:
    return {
        "environment": "live",
        "enabled_exchanges": ["binance"],
        "leverage": 2,
        "notional_per_trade": "100",
        "per_exchange_max_exposure": "500",
        "global_max_exposure": "1000",
        "max_concurrent_positions": 5,
        "minimum_current_apr": "0.10",
        "minimum_apr_24h": "0.08",
        "minimum_apr_7d": "0.05",
        "minimum_net_return": "0.005",
        "minimum_opening_basis": "0",
        "maximum_opening_basis": "0.02",
        "minimum_two_leg_notional": "50",
        "book_capacity_multiple": "2",
        "normal_max_slippage": "0.001",
        "emergency_max_slippage": "0.01",
        "daily_max_loss": "50",
        "minimum_reentry_minutes": 60,
        "maximum_holding_hours": 720,
        "minimum_liquidation_buffer": "0.20",
        "close_funding_rate_below": "0",
        "close_net_return_below": "0.001",
        "close_basis_above": "0.03",
        "take_profit_usdt": "25",
        "stop_loss_usdt": "20",
    }


async def test_automation_is_fail_closed_versioned_and_explicitly_enabled() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    await credentials.save(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="primary",
        secrets=ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        actor="test",
    )
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/api/automation")
        assert initial.status_code == 200
        assert initial.json()["state"] == "disabled"
        assert initial.json()["active_strategy"] is None

        first = await client.put(
            "/api/automation/config",
            json=strategy_config(),
        )
        second_config = {
            **strategy_config(),
            "notional_per_trade": "150",
        }
        second = await client.put(
            "/api/automation/config",
            json=second_config,
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["strategy"]["version"] == 1
        assert second.json()["strategy"]["version"] == 2
        strategy_id = second.json()["strategy"]["id"]
        assert second.json()["strategy"]["config"]["notional_per_trade"] == "150"
        assert second.json()["strategy"]["config"]["minimum_opening_basis"] == "0"

        blocked = await client.post(
            "/api/automation/enable",
            json={"strategy_id": strategy_id, "confirmed": True},
        )
        assert blocked.status_code == 409
        assert "execution is not ready" in blocked.json()["detail"]

        await database.set_execution_control(state="ready", reason="test")
        enabled = await client.post(
            "/api/automation/enable",
            json={"strategy_id": strategy_id, "confirmed": True},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["state"] == "enabled"

        paused = await client.post(
            "/api/automation/pause",
            json={"reason": "operator maintenance"},
        )
        assert paused.json() == {
            "state": "paused",
            "reason": "operator maintenance",
        }
        resumed = await client.post("/api/automation/resume")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "enabled"
        disabled = await client.post("/api/automation/disable")
        assert disabled.status_code == 200
        assert disabled.json()["state"] == "disabled"

        status = await client.get("/api/automation")
        assert status.json()["latest_strategy"]["version"] == 2
        assert status.json()["active_strategy"]["id"] == strategy_id
    first_row = await database.strategy_version(
        first.json()["strategy"]["id"]
    )
    second_row = await database.strategy_version(
        second.json()["strategy"]["id"]
    )
    assert first_row is not None and second_row is not None
    assert first_row.payload != second_row.payload
    await database.close()


async def test_automation_rejects_incomplete_limits_and_missing_credentials() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        invalid = await client.put(
            "/api/automation/config",
            json={
                **strategy_config(),
                "global_max_exposure": "10",
            },
        )
        assert invalid.status_code == 422
        invalid_basis_range = await client.put(
            "/api/automation/config",
            json={
                **strategy_config(),
                "minimum_opening_basis": "0.03",
                "maximum_opening_basis": "0.02",
            },
        )
        assert invalid_basis_range.status_code == 422

        saved = await client.put(
            "/api/automation/config",
            json=strategy_config(),
        )
        await database.set_execution_control(state="ready", reason="test")
        missing = await client.post(
            "/api/automation/enable",
            json={
                "strategy_id": saved.json()["strategy"]["id"],
                "confirmed": True,
            },
        )
        assert missing.status_code == 409
        assert "credentials are missing" in missing.json()["detail"]
    control = await database.automation_control()
    assert control.state == "disabled"
    assert Decimal(
        strategy_config()["notional_per_trade"]
    ) == Decimal("100")
    await database.close()


async def test_gate_sandbox_strategy_can_be_enabled_with_sandbox_credentials() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    await credentials.save(
        exchange=Exchange.GATE,
        environment=ExchangeEnvironment.SANDBOX,
        label="sandbox",
        secrets=ExchangeSecrets(
            api_key="gate-sandbox-key",
            api_secret="gate-sandbox-secret",
        ),
        actor="test",
    )
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
    )
    config = {
        **strategy_config(),
        "environment": "sandbox",
        "enabled_exchanges": ["gate"],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        saved = await client.put("/api/automation/config", json=config)
        assert saved.status_code == 200, saved.text
        await database.set_execution_control(state="ready", reason="test")
        enabled = await client.post(
            "/api/automation/enable",
            json={
                "strategy_id": saved.json()["strategy"]["id"],
                "confirmed": True,
            },
        )

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["state"] == "enabled"
    await database.close()


async def test_global_execution_pause_requires_confirmation_and_fresh_resume() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    await database.set_execution_control(state="ready", reason="test")
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing_confirmation = await client.post(
            "/api/system/execution/pause",
            json={"confirmed": False, "reason": "maintenance"},
        )
        assert missing_confirmation.status_code == 422

        paused = await client.post(
            "/api/system/execution/pause",
            json={"confirmed": True, "reason": "maintenance"},
        )
        assert paused.status_code == 200
        assert paused.json()["state"] == "paused"
        assert paused.json()["cancel_open_orders"] == "worker_pending"

        resumed = await client.post(
            "/api/system/execution/resume",
            json={"confirmed": True},
        )
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "reconciling"

    control = await database.execution_control()
    assert control is not None
    assert control.state == "reconciling"
    await database.close()
