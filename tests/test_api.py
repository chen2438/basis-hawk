from datetime import UTC, datetime
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from basis_hawk.api import create_app
from basis_hawk.models import Exchange, Opportunity, Quality
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


def opportunity() -> Opportunity:
    return Opportunity(
        exchange=Exchange.BINANCE,
        base_asset="BTC",
        spot_symbol="BTCUSDT",
        perp_symbol="BTCUSDT",
        observed_at=datetime.now(UTC),
        spot_ask=Decimal("100"),
        perp_bid=Decimal("101"),
        executable_basis=Decimal("0.01"),
        top_book_notional=Decimal("500"),
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
    app = create_app(service, manage_lifecycle=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/opportunities", params={"exchange": "binance"})
        assert response.status_code == 200
        assert response.json()["items"][0]["spot_ask"] == "100"
        settings = (await client.get("/api/settings")).json()
        settings["holding_period_days"] = 14
        saved = await client.put("/api/settings", json=settings)
        assert saved.status_code == 200
        assert saved.json()["holding_period_days"] == 14
    await database.close()


def test_websocket_starts_with_snapshot() -> None:
    service = ScannerService(Database("sqlite+aiosqlite:///:memory:"), {})
    service.opportunities["binance:BTC"] = opportunity()
    app = create_app(service, manage_lifecycle=False)
    with TestClient(app).websocket_connect("/api/ws/opportunities") as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["items"][0]["base_asset"] == "BTC"
