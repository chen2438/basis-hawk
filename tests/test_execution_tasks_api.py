from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx

from basis_hawk.accounts import (
    AccountSnapshot,
    PerpMarginMode,
    PositionMode,
    RemoteTradingState,
)
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


def _task_payload(
    *,
    environment: str = "paper",
    account_id: str | None = None,
    name: str = "BTC carry",
) -> dict[str, object]:
    return {
        "name": name,
        "display_symbol": "BTC/USDT",
        "environment": environment,
        "base_asset": "BTC",
        "quantity_mode": "base",
        "maximum_base_exposure": "0.01",
        "maximum_notional_exposure_usdt": "1000",
        "legs": [
            {
                "account_id": account_id,
                "exchange": "binance",
                "role": "anchor",
                "market_type": "spot",
                "side": "buy",
                "base_asset": "BTC",
                "symbol": "BTCUSDT",
                "target_quantity": "0.01",
                "order_mode": "maker",
                "maker_policy": {
                    "book_level": 3,
                    "maximum_chases": 50,
                    "fallback_mode": "protected_ioc",
                },
            },
            {
                "account_id": account_id,
                "exchange": "binance",
                "role": "hedge",
                "market_type": "perpetual",
                "side": "sell",
                "base_asset": "BTC",
                "symbol": "BTCUSDT",
                "target_quantity": "0.01",
                "order_mode": "protected_ioc",
                "margin_mode": "isolated",
                "leverage": 2,
            },
        ],
    }


async def test_paper_task_api_is_idempotent_and_requires_fresh_version() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    scanner = ScannerService(database, {})
    await scanner.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    app = create_app(
        scanner,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
    )
    key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": key},
            json=_task_payload(),
        )
        assert created.status_code == 201
        assert created.json()["created"] is True
        task = created.json()["task"]
        task_id = task["id"]
        assert task["status"] == "draft"
        assert len(task["legs"]) == 2

        replay = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": key},
            json=_task_payload(),
        )
        assert replay.status_code == 200
        assert replay.json()["created"] is False
        assert replay.json()["task"]["id"] == task_id

        conflict = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": key},
            json=_task_payload(name="different"),
        )
        assert conflict.status_code == 409

        preflight = await client.post(
            f"/api/v2/execution-tasks/{task_id}/preflight"
        )
        assert preflight.status_code == 200
        ready = preflight.json()["task"]
        assert ready["status"] == "preflight_ready"
        assert ready["preflight"]["paper"] is True
        assert ready["version"] == 2

        stale = await client.post(
            f"/api/v2/execution-tasks/{task_id}/start",
            json={"expected_version": 1, "confirmed": True},
        )
        assert stale.status_code == 409
        started = await client.post(
            f"/api/v2/execution-tasks/{task_id}/start",
            json={"expected_version": 2, "confirmed": True},
        )
        assert started.status_code == 200
        assert started.json()["task"]["status"] == "queued"

        listing = await client.get("/api/v2/execution-tasks")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["id"] == task_id

        activity = await client.get(
            f"/api/v2/execution-tasks/{task_id}/activity"
        )
        assert activity.status_code == 200
        assert activity.json()["activity"] == {
            "runs": [],
            "orders": [],
            "fills": [],
        }
        missing_activity = await client.get(
            f"/api/v2/execution-tasks/{uuid4()}/activity"
        )
        assert missing_activity.status_code == 404

        strategies = await client.get("/api/v2/strategies?status=running")
        assert strategies.status_code == 200
        assert strategies.json() == {"items": []}
        invalid_status = await client.get(
            "/api/v2/strategies?status=not-a-status"
        )
        assert invalid_status.status_code == 422

        cancelable = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": str(uuid4())},
            json=_task_payload(name="cancel before start"),
        )
        cancelable_id = cancelable.json()["task"]["id"]
        canceled = await client.post(
            f"/api/v2/execution-tasks/{cancelable_id}/cancel",
            json={"expected_version": 1},
        )
        assert canceled.status_code == 200
        assert canceled.json()["task"]["status"] == "emergency_stopped"
        cannot_start = await client.post(
            f"/api/v2/execution-tasks/{cancelable_id}/start",
            json={"expected_version": 2, "confirmed": True},
        )
        assert cannot_start.status_code == 409
    await database.close()


async def test_live_task_preflight_is_account_scoped_and_sanitized() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    scanner = ScannerService(database, {})
    await scanner.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    account = await credentials.create_account(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="main",
        secrets=ExchangeSecrets(
            api_key="private-api-key",
            api_secret="private-api-secret",
        ),
        actor="admin",
    )
    await database.set_execution_control(state="ready", reason="test ready")

    class FakeAccountClient:
        async def snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                spot_usdt_available=Decimal("1000"),
                perp_usdt_available=Decimal("1000"),
                perp_usdt_equity=Decimal("1000"),
                shared_balance=False,
                account_mode="spot+usdt_futures",
                position_mode=PositionMode.ONE_WAY,
                trade_permission=True,
                perp_margin_mode=PerpMarginMode.ISOLATED,
            )

        async def trading_state(self) -> RemoteTradingState:
            return RemoteTradingState(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                open_orders=[],
                positions=[],
                complete=True,
            )

        async def close(self) -> None:
            return None

    def account_factory(exchange, secrets, environment):
        assert exchange == Exchange.BINANCE
        assert secrets.api_key == "private-api-key"
        assert environment == ExchangeEnvironment.LIVE
        return FakeAccountClient()

    app = create_app(
        scanner,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
        account_client_factory=account_factory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        wrong_environment = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": str(uuid4())},
            json=_task_payload(
                environment="sandbox",
                account_id=account.id,
            ),
        )
        assert wrong_environment.status_code == 422

        wrong_exchange_payload = _task_payload(
            environment="live",
            account_id=account.id,
        )
        wrong_exchange_payload["legs"][0]["exchange"] = "okx"
        wrong_exchange = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": str(uuid4())},
            json=wrong_exchange_payload,
        )
        assert wrong_exchange.status_code == 422

        created = await client.post(
            "/api/v2/execution-tasks",
            headers={"Idempotency-Key": str(uuid4())},
            json=_task_payload(
                environment="live",
                account_id=account.id,
            ),
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]
        preflight = await client.post(
            f"/api/v2/execution-tasks/{task_id}/preflight"
        )
        assert preflight.status_code == 200
        payload = preflight.json()["task"]["preflight"]
        assert payload["accounts"][0]["account_id"] == account.id
        assert payload["accounts"][0]["position_mode"] == "one_way"
        assert "private-api-key" not in preflight.text
        assert "private-api-secret" not in preflight.text

        started = await client.post(
            f"/api/v2/execution-tasks/{task_id}/start",
            json={
                "expected_version": preflight.json()["task"]["version"],
                "confirmed": True,
            },
        )
        assert started.status_code == 200
        assert started.json()["task"]["status"] == "queued"
    await database.close()
