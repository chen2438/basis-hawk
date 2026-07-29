import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from basis_hawk.accounts import AccountSnapshot, PositionMode, RemoteFill
from basis_hawk.api import create_app
from basis_hawk.config import get_config
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import (
    Exchange,
    ExchangeStatus,
    InstrumentPair,
    Opportunity,
    Quality,
)
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database
from basis_hawk.trading import PaperExecutionService, TradeLedger


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


def instrument_pair() -> InstrumentPair:
    return InstrumentPair(
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


async def create_live_position(database: Database) -> str:
    intent, _ = await TradeLedger(database).plan_live_open(
        opportunity=opportunity(),
        pair=instrument_pair(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=await database.load_settings(),
        environment=ExchangeEnvironment.LIVE,
        leverage=2,
    )
    stored = await database.trade_intent(intent.id)
    assert stored is not None
    now = datetime.now(UTC)
    for leg in stored[1]:
        await database.persist_remote_fills(
            order_leg_id=leg.id,
            fills=[
                RemoteFill(
                    exchange_trade_id=f"fill-{leg.market}",
                    exchange_order_id=f"order-{leg.market}",
                    client_order_id=leg.client_order_id,
                    market=leg.market,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    price=leg.limit_price,
                    fee_amount=Decimal("0.01"),
                    fee_asset="USDT",
                    liquidity="taker",
                    occurred_at=now,
                )
            ],
        )
    async with database.sessions() as session:
        row = await session.get(type(stored[0]), intent.id)
        assert row is not None
        row.status = "executing"
        await session.commit()
    settled = await database.settle_live_open(intent_id=intent.id)
    assert settled is not None and settled[1] is not None
    return settled[1].id


async def test_rest_contract_and_settings() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.statuses[Exchange.BINANCE] = ExchangeStatus(
        exchange=Exchange.BINANCE,
        state="healthy",
        instruments=20,
        history_ready=5,
        history_progress_percent=25.0,
        history_download_rate_per_minute=12.5,
        history_syncing=True,
    )
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await client.get("/api/auth/session")
        assert session.json() == {"username": "local"}
        execution = await client.get("/api/system/execution")
        assert execution.json()["state"] == "blocked"
        assert execution.json()["accounts"] == []
        exchange_status = (await client.get("/api/exchanges/status")).json()["items"][0]
        assert exchange_status["history_ready"] == 5
        assert exchange_status["history_progress_percent"] == 25.0
        assert exchange_status["history_download_rate_per_minute"] == 12.5
        assert exchange_status["history_syncing"] is True
        response = await client.get("/api/opportunities", params={"exchange": "binance"})
        assert response.status_code == 200
        assert response.json()["items"][0]["spot_ask"] == "100"
        top_book = await client.get("/api/opportunities/binance/BTC/top-book")
        assert top_book.status_code == 200
        assert top_book.json()["spot_ask_notional"] == "0"
        assert top_book.json()["perp_bid_notional"] == "0"
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
        assert fetched.json()["intent"]["failure_code"] is None
        assert fetched.json()["intent"]["legs"][0]["client_order_id"].startswith(
            "bh-"
        )
        result = await PaperExecutionService(database).run_once()
        assert result.executed == 1
        await database.persist_funding_income(
            exchange="binance",
            environment="paper",
            records=[
                {
                    "exchange_record_id": "position-funding-1",
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "asset": "USDT",
                    "amount": Decimal("0.5"),
                    "rate": Decimal("0.0001"),
                    "position_value": Decimal("100"),
                    "occurred_at": datetime.now(UTC),
                }
            ],
        )
        fills = await client.get(f"/api/trades/intents/{intent_id}/fills")
        assert len(fills.json()["items"]) == 2
        positions = await client.get(
            "/api/trades/positions",
            params={"status": "open", "include_valuation": True},
        )
        open_position = positions.json()["items"][0]
        position_id = open_position["id"]
        assert open_position["opening_intent_id"] == intent_id
        assert Decimal(open_position["notional_usdt"]) == Decimal("100")
        assert open_position["leverage"] == 1
        assert open_position["spot_exit_price"] == "99"
        assert open_position["perp_exit_price"] == "102"
        assert Decimal(open_position["unrealized_pnl_usdt"]) == (
            Decimal(open_position["quantity"]) * Decimal("-2")
        )
        expected_closing_fees = Decimal(open_position["quantity"]) * (
            Decimal("99") * Decimal(open_position["spot_fee_rate"])
            + Decimal("102") * Decimal(open_position["perp_fee_rate"])
        )
        assert Decimal(open_position["funding_income_usdt"]) == Decimal("0.5")
        assert (
            Decimal(open_position["estimated_closing_fees_usdt"])
            == expected_closing_fees
        )
        assert Decimal(open_position["estimated_final_pnl_usdt"]) == (
            Decimal(open_position["unrealized_pnl_usdt"])
            + Decimal("0.5")
            - Decimal(open_position["remaining_opening_fees_usdt"])
            - expected_closing_fees
        )
        assert open_position["valuation_observed_at"] is not None
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
        assert closed.json()["items"][0]["unrealized_pnl_usdt"] is None
    await database.close()


async def test_global_trade_ledgers_are_bounded_filterable_and_decimal_safe() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    ledger = TradeLedger(database)
    opening, _ = await ledger.plan_paper_open(
        opportunity=opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=await database.load_settings(),
    )
    executor = PaperExecutionService(database)
    assert (await executor.run_once()).executed == 1
    [position] = await ledger.positions(status="open")
    closing, _ = await ledger.plan_paper_close(
        position_id=position.id,
        opportunity=opportunity(),
        idempotency_key=uuid.uuid4(),
        settings=await database.load_settings(),
    )
    assert (await executor.run_once()).executed == 1
    await database.persist_funding_income(
        exchange="binance",
        environment="paper",
        records=[
            {
                "exchange_record_id": "fund-1",
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "asset": "USDT",
                "amount": Decimal("0.25"),
                "rate": Decimal("0.0001"),
                "position_value": Decimal("2500"),
                "occurred_at": datetime(2026, 7, 26, 12, tzinfo=UTC),
            }
        ],
    )

    app = create_app(service, manage_lifecycle=False, auth_required=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        intents = await client.get(
            "/api/trades/intents",
            params={"status": "closed", "limit": 1},
        )
        assert intents.status_code == 200
        assert [item["id"] for item in intents.json()["items"]] == [closing.id]
        assert intents.json()["items"][0]["failure_code"] is None
        assert "activity_at" in intents.json()["items"][0]

        orders = await client.get(
            "/api/trades/orders",
            params={"status": "filled", "limit": 10},
        )
        assert orders.status_code == 200
        assert len(orders.json()["items"]) == 4
        assert all(item["failure_code"] is None for item in orders.json()["items"])
        assert {item["trade_intent_id"] for item in orders.json()["items"]} == {
            opening.id,
            closing.id,
        }
        assert all(isinstance(item["quantity"], str) for item in orders.json()["items"])

        fills = await client.get("/api/trades/fills", params={"limit": 10})
        assert fills.status_code == 200
        assert len(fills.json()["items"]) == 4
        assert all(isinstance(item["price"], str) for item in fills.json()["items"])
        assert {item["leg"] for item in fills.json()["items"]} == {"spot", "perp"}

        pnl = await client.get("/api/trades/pnl", params={"limit": 10})
        assert pnl.status_code == 200
        [realization] = pnl.json()["items"]
        assert realization["paired_position_id"] == position.id
        assert realization["closing_intent_id"] == closing.id
        assert isinstance(realization["net_pnl_usdt"], str)

        funding_income = await client.get(
            "/api/trades/funding-income",
            params={"exchange": "binance", "limit": 10},
        )
        assert funding_income.status_code == 200
        [funding_item] = funding_income.json()["items"]
        assert funding_item["exchange_record_id"] == "fund-1"
        assert funding_item["amount"] == "0.250000000000000000"

        too_many = await client.get("/api/trades/orders", params={"limit": 501})
        assert too_many.status_code == 422
    await database.close()


async def test_live_open_requires_persisted_preview_and_confirmation() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.pairs[Exchange.BINANCE] = [instrument_pair()]
    executable_quote_requests: list[tuple[Exchange, str]] = []

    async def executable_opportunity(
        exchange: Exchange,
        base_asset: str,
    ) -> Opportunity | None:
        executable_quote_requests.append((exchange, base_asset))
        return service.opportunities.get(f"{exchange.value}:{base_asset}")

    service.executable_opportunity = executable_opportunity  # type: ignore[method-assign]
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
    await database.set_execution_control(
        state="ready",
        reason="credential reconciliation passed",
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
        too_small = await client.post(
            "/api/trades/open/preview",
            json={
                "exchange": "binance",
                "environment": "live",
                "base_asset": "btc",
                "notional_usdt": "1",
            },
        )
        assert too_small.status_code == 409
        assert too_small.json()["detail"] == {
            "code": "notional_below_minimum",
            "message": "requested notional is below the minimum executable amount",
            "minimum_notional_usdt": "5.00",
        }
        too_large = await client.post(
            "/api/trades/open/preview",
            json={
                "exchange": "binance",
                "environment": "live",
                "base_asset": "btc",
                "notional_usdt": "501",
            },
        )
        assert too_large.status_code == 409
        assert too_large.json()["detail"] == {
            "code": "notional_exceeds_top_book",
            "message": "notional exceeds current top-book capacity",
            "capacity_notional_usdt": "500",
        }
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
        assert executable_quote_requests == [
            (Exchange.BINANCE, "BTC"),
            (Exchange.BINANCE, "BTC"),
            (Exchange.BINANCE, "BTC"),
        ]
        preview_id = preview_response.json()["preview_id"]
        preview = preview_response.json()["preview"]
        assert "request_fingerprint" not in preview
        assert "config_version" not in preview
        preview_lifetime = (
            datetime.fromisoformat(preview["expires_at"])
            - datetime.fromisoformat(preview["market_observed_at"])
        )
        assert preview_lifetime.total_seconds() >= 59
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


async def test_live_close_requires_position_bound_preview_and_confirmation() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.pairs[Exchange.BINANCE] = [instrument_pair()]
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
    position_id = await create_live_position(database)
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
        positions = await client.get(
            "/api/trades/positions",
            params={"status": "open"},
        )
        assert positions.status_code == 200
        assert Decimal(
            positions.json()["items"][0]["notional_usdt"]
        ).quantize(Decimal("0.1")) == Decimal("100.1")
        assert positions.json()["items"][0]["leverage"] == 2
        preview_response = await client.post(
            f"/api/trades/positions/{position_id}/close/preview",
            json={"maximum_slippage": "0.002"},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview_id = preview_response.json()["preview_id"]
        preview = preview_response.json()["preview"]
        assert preview["position_id"] == position_id
        assert Decimal(preview["base_quantity"]) == Decimal("1")
        assert Decimal(preview["spot_quantity"]) == Decimal("1")
        assert Decimal(preview["perp_quantity"]) == Decimal("1")
        assert Decimal(preview["spot_limit_price"]) < Decimal("99")
        assert Decimal(preview["perp_limit_price"]) > Decimal("102")
        assert Decimal(preview["estimated_total_fees_usdt"]) > 0
        assert "request_fingerprint" not in preview
        assert "config_version" not in preview
        position = await database.paired_position(position_id)
        assert position is not None and position.status == "open"

        unconfirmed = await client.post(
            f"/api/trades/positions/{position_id}/close/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"preview_id": preview_id, "confirmed": False},
        )
        assert unconfirmed.status_code == 422
        await database.set_execution_control(state="ready", reason="test")
        service.opportunities["binance:BTC"] = opportunity().model_copy(
            update={
                "spot_bid": Decimal("98.9"),
                "perp_ask": Decimal("102.1"),
            }
        )
        key = str(uuid.uuid4())
        confirmed = await client.post(
            f"/api/trades/positions/{position_id}/close/confirm",
            headers={"Idempotency-Key": key},
            json={"preview_id": preview_id, "confirmed": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["created"] is True
        intent = confirmed.json()["intent"]
        assert intent["action"] == "close"
        assert intent["paired_position_id"] == position_id
        legs = {item["market"]: item for item in intent["legs"]}
        assert legs["spot"]["side"] == "sell"
        assert legs["spot"]["reduce_only"] is False
        assert Decimal(legs["spot"]["limit_price"]).quantize(
            Decimal("0.01")
        ) == Decimal(preview["spot_limit_price"])
        assert legs["perp"]["side"] == "buy"
        assert legs["perp"]["reduce_only"] is True
        assert Decimal(legs["perp"]["limit_price"]).quantize(
            Decimal("0.01")
        ) == Decimal(preview["perp_limit_price"])
        position = await database.paired_position(position_id)
        assert position is not None
        assert position.status == "closing"
        assert position.closing_intent_id == intent["id"]

        repeated = await client.post(
            f"/api/trades/positions/{position_id}/close/confirm",
            headers={"Idempotency-Key": key},
            json={"preview_id": preview_id, "confirmed": True},
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
    await database.close()


async def test_emergency_close_allows_degraded_quote_and_pauses_execution() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity().model_copy(
        update={"quality": Quality.WARMING}
    )
    service.pairs[Exchange.BINANCE] = [instrument_pair()]
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
    position_id = await create_live_position(database)
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
        normal = await client.post(
            f"/api/trades/positions/{position_id}/close/preview",
            json={"maximum_slippage": "0.2"},
        )
        assert normal.status_code == 409

        preview_response = await client.post(
            f"/api/trades/positions/{position_id}/close/preview",
            json={
                "emergency": True,
                "maximum_slippage": "0.2",
            },
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()["preview"]
        assert preview["emergency"] is True
        assert Decimal(preview["maximum_slippage"]) == Decimal("0.2")
        stored_preview = await database.trade_preview(
            preview_response.json()["preview_id"]
        )
        assert stored_preview is not None
        assert stored_preview.emergency is True
        assert stored_preview.maximum_slippage.quantize(
            Decimal("0.000000000001")
        ) == Decimal("0.2")

        confirmed = await client.post(
            f"/api/trades/positions/{position_id}/close/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_id": preview_response.json()["preview_id"],
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["intent"]["emergency"] is True
        assert confirmed.json()["intent"]["action"] == "close"
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert "emergency paired close" in control.reason
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
        assert (
            confirmed.json()["detail"]
            == "market moved beyond preview slippage protection"
        )
    await database.close()


async def test_live_confirmation_accepts_refreshed_market_timestamp() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    service.opportunities["binance:BTC"] = opportunity()
    service.pairs[Exchange.BINANCE] = [instrument_pair()]
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
        assert preview.status_code == 200, preview.text
        original = preview.json()["preview"]
        await database.set_execution_control(state="ready", reason="test")
        service.opportunities["binance:BTC"] = opportunity().model_copy(
            update={
                "spot_ask": Decimal("100.05"),
                "perp_bid": Decimal("100.95"),
            }
        )
        confirmed = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_id": preview.json()["preview_id"],
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["created"] is True
        legs = {
            item["market"]: item
            for item in confirmed.json()["intent"]["legs"]
        }
        assert Decimal(legs["spot"]["limit_price"]).quantize(
            Decimal("0.01")
        ) == Decimal(original["spot_limit_price"])
        assert Decimal(legs["perp"]["limit_price"]).quantize(
            Decimal("0.01")
        ) == Decimal(original["perp_limit_price"])

        rules_preview = await client.post(
            "/api/trades/open/preview",
            json={
                "exchange": "binance",
                "environment": "live",
                "base_asset": "BTC",
                "notional_usdt": "100",
            },
        )
        assert rules_preview.status_code == 200, rules_preview.text
        service.pairs[Exchange.BINANCE] = [
            instrument_pair().model_copy(
                update={"spot_price_increment": Decimal("0.1")}
            )
        ]
        changed_rules = await client.post(
            "/api/trades/open/confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_id": rules_preview.json()["preview_id"],
                "confirmed": True,
            },
        )
        assert changed_rules.status_code == 409
        assert (
            changed_rules.json()["detail"]
            == "market or configuration changed after trade preview"
        )
    await database.close()


def test_websocket_starts_with_snapshot() -> None:
    service = ScannerService(Database("sqlite+aiosqlite:///:memory:"), {})
    service.opportunities["binance:BTC"] = opportunity()
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    with TestClient(app).websocket_connect("/api/ws/opportunities") as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["items"][0]["base_asset"] == "BTC"


class _TransferPreflightClient:
    async def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal("100"),
            perp_usdt_available=Decimal("20"),
            perp_usdt_equity=Decimal("20"),
            shared_balance=False,
            account_mode="classic",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def close(self) -> None:
        return None


async def test_internal_transfer_api_requires_confirmation_and_is_idempotent(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "BASIS_HAWK_TRANSFER_PER_REQUEST_LIMIT_USDT",
        "100",
    )
    monkeypatch.setenv("BASIS_HAWK_TRANSFER_DAILY_LIMIT_USDT", "200")
    get_config.cache_clear()
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {})
    await service.initialize()
    await database.set_execution_control(state="ready", reason="test")
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
    await database.set_execution_control(
        state="ready",
        reason="credential reconciliation passed",
    )
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
        account_client_factory=lambda *_: _TransferPreflightClient(),  # type: ignore[arg-type]
    )
    key = str(uuid.uuid4())
    payload = {
        "exchange": "binance",
        "environment": "live",
        "direction": "spot_to_perp",
        "amount_usdt": "25",
        "confirmed": True,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            initial_limits = await client.get("/api/transfers/limits")
            assert initial_limits.status_code == 200
            assert initial_limits.json() == {
                "per_request_limit_usdt": "100",
                "daily_limit_usdt": "200",
                "enabled": True,
                "updated_by": "environment",
                "updated_at": initial_limits.json()["updated_at"],
            }
            missing_confirmation = await client.put(
                "/api/transfers/limits",
                json={
                    "per_request_limit_usdt": "50",
                    "daily_limit_usdt": "150",
                    "confirmed": False,
                },
            )
            assert missing_confirmation.status_code == 422
            updated_limits = await client.put(
                "/api/transfers/limits",
                json={
                    "per_request_limit_usdt": "50",
                    "daily_limit_usdt": "150",
                    "confirmed": True,
                },
            )
            assert updated_limits.status_code == 200
            assert updated_limits.json()["per_request_limit_usdt"] == "50"
            assert updated_limits.json()["daily_limit_usdt"] == "150"
            assert updated_limits.json()["updated_by"] == "local"
            over_new_limit = await client.post(
                "/api/transfers",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={**payload, "amount_usdt": "51"},
            )
            assert over_new_limit.status_code == 409
            assert (
                over_new_limit.json()["detail"]
                == "internal transfer exceeds the per-request limit"
            )
            created = await client.post(
                "/api/transfers",
                headers={"Idempotency-Key": key},
                json=payload,
            )
            assert created.status_code == 200
            assert created.json()["created"] is True
            assert created.json()["transfer"]["status"] == "planned"
            repeated = await client.post(
                "/api/transfers",
                headers={"Idempotency-Key": key},
                json=payload,
            )
            assert repeated.status_code == 200
            assert repeated.json()["created"] is False
            listed = await client.get("/api/transfers")
            assert listed.json()["items"][0]["amount_usdt"] == "25"
        control = await database.execution_control()
        assert control is not None and control.state == "paused"
        audits = await database.audit_events(
            event_type="transfer.limits_updated",
        )
        assert len(audits) == 1
        assert audits[0].details == {
            "daily_limit_usdt": "150",
            "enabled": True,
            "per_request_limit_usdt": "50",
        }
    finally:
        await database.close()
        get_config.cache_clear()
