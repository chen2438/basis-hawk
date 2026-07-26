from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.exchanges.base import ExchangeAdapter
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


class FakeAdapter(ExchangeAdapter):
    name = "binance"

    async def instruments(self):
        return [
            InstrumentPair(
                exchange=Exchange.BINANCE,
                base_asset="BTC",
                spot_symbol="BTCUSDT",
                perp_symbol="BTCUSDT",
            )
        ]

    async def quotes(self, pairs):
        return [
            MarketQuote(
                exchange=Exchange.BINANCE,
                base_asset="BTC",
                observed_at=datetime.now(UTC),
                spot_bid=Decimal("99"),
                spot_bid_qty=Decimal("5"),
                spot_ask=Decimal("100"),
                spot_ask_qty=Decimal("5"),
                perp_bid=Decimal("101"),
                perp_bid_qty=Decimal("5"),
                perp_ask=Decimal("102"),
                perp_ask_qty=Decimal("5"),
                spot_quote_volume_24h=Decimal("2000000"),
                perp_quote_volume_24h=Decimal("3000000"),
            )
        ]

    async def current_funding(self, pairs):
        return [
            FundingObservation(
                exchange=Exchange.BINANCE,
                base_asset="BTC",
                rate=Decimal("0.0001"),
                funding_at=datetime.now(UTC),
            )
        ]

    async def funding_history(self, pair, *, start, end):
        return [
            FundingObservation(
                exchange=Exchange.BINANCE,
                base_asset="BTC",
                rate=Decimal("0.0001"),
                funding_at=end - timedelta(hours=8 * offset),
                settled=True,
            )
            for offset in range(22)
        ]

    async def close(self):
        return None


async def test_refreshes_and_recalculates() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {Exchange.BINANCE: FakeAdapter()})
    await service.initialize()
    await service.refresh_catalog(Exchange.BINANCE)
    await service.refresh_current_funding(Exchange.BINANCE)
    history = await service.adapters[Exchange.BINANCE].funding_history(
        service.pairs[Exchange.BINANCE][0],
        start=datetime.now(UTC) - timedelta(days=8),
        end=datetime.now(UTC),
    )
    service.history["binance:BTC"] = history
    await service.refresh_quotes(Exchange.BINANCE)
    result = service.list_opportunities()[0]
    assert result.base_asset == "BTC"
    assert result.net_return is not None
    await database.close()
