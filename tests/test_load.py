from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

import httpx
from sqlalchemy import func, select

from basis_hawk.api import create_app
from basis_hawk.exchanges.base import ExchangeAdapter
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database, LatestOpportunityRow


class LoadAdapter(ExchangeAdapter):
    def __init__(self, exchange: Exchange, count: int = 600) -> None:
        self.exchange = exchange
        self.name = exchange.value
        self.count = count

    async def instruments(self) -> list[InstrumentPair]:
        return [
            InstrumentPair(
                exchange=self.exchange,
                base_asset=f"A{index:04d}",
                spot_symbol=f"A{index:04d}USDT",
                perp_symbol=f"A{index:04d}USDT",
                spot_price_increment=Decimal("0.01"),
                spot_quantity_increment=Decimal("0.01"),
                spot_min_quantity=Decimal("0.01"),
                spot_min_notional=Decimal("5"),
                perp_price_increment=Decimal("0.01"),
                perp_quantity_increment=Decimal("0.01"),
                perp_min_quantity=Decimal("0.01"),
                perp_min_notional=Decimal("5"),
                perp_contract_size=Decimal("1"),
            )
            for index in range(self.count)
        ]

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        observed_at = datetime.now(UTC)
        return [
            MarketQuote(
                exchange=self.exchange,
                base_asset=pair.base_asset,
                observed_at=observed_at,
                spot_bid=Decimal("99"),
                spot_bid_qty=Decimal("1000"),
                spot_ask=Decimal("100"),
                spot_ask_qty=Decimal("1000"),
                perp_bid=Decimal("101"),
                perp_bid_qty=Decimal("1000"),
                perp_ask=Decimal("102"),
                perp_ask_qty=Decimal("1000"),
                spot_quote_volume_24h=Decimal("2000000") + index,
                perp_quote_volume_24h=Decimal("3000000") + index,
            )
            for index, pair in enumerate(pairs)
        ]

    async def current_funding(
        self,
        pairs: list[InstrumentPair],
    ) -> list[FundingObservation]:
        now = datetime.now(UTC)
        return [
            FundingObservation(
                exchange=self.exchange,
                base_asset=pair.base_asset,
                rate=Decimal("0.0001"),
                funding_at=now,
            )
            for pair in pairs
        ]

    async def funding_history(
        self,
        pair: InstrumentPair,
        *,
        start: datetime,
        end: datetime,
    ) -> list[FundingObservation]:
        return []

    async def close(self) -> None:
        return None


async def test_six_exchange_universe_handles_three_thousand_candidates() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    adapters = {exchange: LoadAdapter(exchange) for exchange in Exchange}
    service = ScannerService(database, adapters)
    await service.initialize()
    started = monotonic()

    for exchange in Exchange:
        await service.refresh_catalog(exchange)
        assert len(service.pairs[exchange]) == 500

    now = datetime.now(UTC)
    settled_history = [
        FundingObservation(
            exchange=Exchange.BINANCE,
            base_asset="SHARED",
            rate=Decimal("0.0001"),
            funding_at=now - timedelta(hours=8 * offset),
            settled=True,
        )
        for offset in range(22)
    ]
    for exchange in Exchange:
        for pair in service.pairs[exchange]:
            service.history[pair.key] = settled_history
        await service.refresh_current_funding(exchange)
        await service.refresh_quotes(exchange)

    values = service.list_opportunities()
    elapsed = monotonic() - started
    assert len(values) == 3000
    assert all(item.quality.value == "healthy" for item in values)
    assert len(await database.latest_opportunities()) == 3000
    async with database.sessions() as session:
        cache_rows = await session.scalar(
            select(func.count()).select_from(LatestOpportunityRow)
        )
    assert cache_rows == 6
    assert elapsed < 20

    app = create_app(service, manage_lifecycle=False, auth_required=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/opportunities",
            params={"page_size": 3000},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 3000
        assert len(response.json()["items"]) == 3000
        rejected = await client.get(
            "/api/opportunities",
            params={"page_size": 3001},
        )
        assert rejected.status_code == 422
    await database.close()
