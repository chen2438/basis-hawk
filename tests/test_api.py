import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from basis_hawk.api import create_app
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import (
    Exchange,
    InstrumentPair,
    Opportunity,
    Quality,
)
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database
from basis_hawk.trading import PaperExecutionService


def opportunity() -> Opportunity:
    return Opportunity(
        exchange=Exchange.BINANCE,
        base_asset="BTC",
        spot_symbol="BTCUSDT",
        perp_symbol="BTCUSDT",
        observed_at=datetime.now(UTC),
        spot_bid=Decimal("99"),
        spot_ask=Decimal("100"),
        perp_bid=Decimal("101"),
        perp_ask=Decimal("102"),
        executable_basis=Decimal("0.01"),
        top_book_notional=Decimal("500"),
        close_top_book_notional=Decimal("500"),
        current_funding_rate=Decimal("0.0001"),
        funding_interval_hours=Decimal("8"),
        next_funding_at=None,
        current_apr=Decimal("0.1095"),
        apr_24h=Decimal("0.1095"),
        apr_7d=Decimal("0.1095"),
        net_return=Decimal("0.006"),
        spot_quote_volume_24h=Decimal("2000000"),
        perp_quote_volume_24h=Decimal("3000000"),
        spot_taker_fee=Decimal("0.001"),
        perp_taker_fee=Decimal("0.0005"),
        quality=Quality.HEALTHY,
    )


async def test_rest_contract_and_settings() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await client.get("/api/auth/session")
        assert session.json() == {"username": "local"}
        execution = await client.get("/api/system/execution")
        assert execution.json()["state"] == "blocked"
        assert execution.json()["accounts"] == []
        response = await client.get("/api/opportunities", params={"exchange": "binance"})
        assert response.status_code == 200
        assert response.json()["items"][0]["spot_ask"] == "100"
        settings = (await client.get("/api/settings")).json()
        settings["holding_period_days"] = 14
        saved = await client.put("/api/settings", json=settings)
        assert saved.status_code == 200
        assert saved.json()["holding_period_days"] == 14
        idempotency_key = str(uuid.uuid4())
        planned = await client.post(
            "/api/trades/paper/open",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "exchange": "binance",
                "base_asset": "btc",
                "notional_usdt": "100",
            },
        )
        assert planned.status_code == 200
        assert planned.json()["created"] is True
        assert planned.json()["intent"]["status"] == "planned"
        intent_id = planned.json()["intent"]["id"]
        repeated = await client.post(
            "/api/trades/paper/open",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "exchange": "binance",
                "base_asset": "BTC",
                "notional_usdt": "100",
            },
        )
        assert repeated.json()["created"] is False
        fetched = await client.get(f"/api/trades/intents/{intent_id}")
        assert fetched.json()["intent"]["legs"][0]["client_order_id"].startswith(
            "bh-"
        )
        result = await PaperExecutionService(database).run_once()
        assert result.executed == 1
        fills = await client.get(f"/api/trades/intents/{intent_id}/fills")
        assert len(fills.json()["items"]) == 2
        positions = await client.get("/api/trades/positions", params={"status": "open"})
        position_id = positions.json()["items"][0]["id"]
        assert positions.json()["items"][0]["opening_intent_id"] == intent_id
        close = await client.post(
            f"/api/trades/paper/positions/{position_id}/close",
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert close.status_code == 200
        assert close.json()["intent"]["action"] == "close"
        assert next(
            leg
            for leg in close.json()["intent"]["legs"]
            if leg["market"] == "perp"
        )["reduce_only"] is True
        assert (await PaperExecutionService(database).run_once()).executed == 1
        closed = await client.get("/api/trades/positions", params={"status": "closed"})
        assert closed.json()["items"][0]["id"] == position_id
    await database.close()


async def test_live_open_requires_persisted_preview_and_confirmation() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.pairs[Exchange.BINANCE] = [
        InstrumentPair(
            exchange=Exchange.BINANCE,
            base_asset="BTC",
            spot_symbol="BTCUSDT",
            perp_symbol="BTCUSDT",
            spot_price_increment=Decimal("0.01"),
            spot_quantity_increment=Decimal("0.001"),
            spot_min_quantity=Decimal("0.001"),
            spot_min_notional=Decimal("5"),
            perp_price_increment=Decimal("0.01"),
            perp_quantity_increment=Decimal("0.001"),
            perp_min_quantity=Decimal("0.001"),
            perp_min_notional=Decimal("5"),
            perp_contract_size=Decimal("1"),
        )
    ]
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
        preview_response = await client.post(
            "/api/trades/open/preview",
            json={
                "exchange": "binance",
                "environment": "live",
                "base_asset": "btc",
                "notional_usdt": "100",
                "leverage": 2,
                "maximum_slippage": "0.001",
            },
        )
        assert preview_response.status_code == 200
        preview_id = preview_response.json()["preview_id"]
        preview = preview_response.json()["preview"]
        assert "request_fingerprint" not in preview
        assert "config_version" not in preview
        assert preview["spot_quantity"] == "1"
        assert preview["perp_quantity"] == "1"
        assert Decimal(preview["spot_limit_price"]) > Decimal("100")
        assert Decimal(preview["perp_limit_price"]) < Decimal("101")
        assert Decimal(preview["estimated_total_fees_usdt"]) > 0
        assert await database.recoverable_trade_intents() == []

        unconfirmed = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"preview_id": preview_id, "confirmed": False},
        )
        assert unconfirmed.status_code == 422
        await database.set_execution_control(state="ready", reason="test")
        idempotency_key = str(uuid.uuid4())
        confirmed = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": idempotency_key},
            json={"preview_id": preview_id, "confirmed": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["created"] is True
        assert confirmed.json()["intent"]["environment"] == "live"
        assert confirmed.json()["intent"]["status"] == "planned"

        repeated = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": idempotency_key},
            json={"preview_id": preview_id, "confirmed": True},
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
        conflict = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"preview_id": preview_id, "confirmed": True},
        )
        assert conflict.status_code == 409
    await database.close()


async def test_live_confirmation_rejects_changed_market() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.pairs[Exchange.BINANCE] = [
        InstrumentPair(
            exchange=Exchange.BINANCE,
            base_asset="BTC",
            spot_symbol="BTCUSDT",
            perp_symbol="BTCUSDT",
            spot_price_increment=Decimal("0.01"),
            spot_quantity_increment=Decimal("0.001"),
            spot_min_quantity=Decimal("0.001"),
            spot_min_notional=Decimal("5"),
            perp_price_increment=Decimal("0.01"),
            perp_quantity_increment=Decimal("0.001"),
            perp_min_quantity=Decimal("0.001"),
            perp_min_notional=Decimal("5"),
            perp_contract_size=Decimal("1"),
        )
    ]
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
        preview = await client.post(
            "/api/trades/open/preview",
            json={
                "exchange": "binance",
                "environment": "live",
                "base_asset": "BTC",
                "notional_usdt": "100",
            },
        )
        await database.set_execution_control(state="ready", reason="test")
        service.opportunities["binance:BTC"] = opportunity().model_copy(
            update={"spot_ask": Decimal("100.5")}
        )
        confirmed = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_id": preview.json()["preview_id"],
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 409
        assert "changed after trade preview" in confirmed.json()["detail"]
    await database.close()


def test_websocket_starts_with_snapshot() -> None:
    service = ScannerService(Database("sqlite+aiosqlite:///:memory:"), {})
    service.opportunities["binance:BTC"] = opportunity()
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    with TestClient(app).websocket_connect("/api/ws/opportunities") as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["items"][0]["base_asset"] == "BTC"
