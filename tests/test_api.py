import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from basis_hawk.api import create_app
from basis_hawk.models import Exchange, Opportunity, Quality
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
        assert positions.json()["items"][0]["opening_intent_id"] == intent_id
    await database.close()


def test_websocket_starts_with_snapshot() -> None:
    service = ScannerService(Database("sqlite+aiosqlite:///:memory:"), {})
    service.opportunities["binance:BTC"] = opportunity()
    app = create_app(service, manage_lifecycle=False, auth_required=False)
    with TestClient(app).websocket_connect("/api/ws/opportunities") as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["items"][0]["base_asset"] == "BTC"
