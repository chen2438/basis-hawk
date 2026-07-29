from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from basis_hawk.exchanges.base import ExchangeAdapter
from basis_hawk.models import (
    Exchange,
    FundingObservation,
    InstrumentPair,
    MarketQuote,
    Opportunity,
    ScannerSettings,
)
from basis_hawk.service import ScannerService, history_snapshot_bucket
from basis_hawk.storage import Database, InstrumentRow


class FakeAdapter(ExchangeAdapter):
    name = "binance"

    async def instruments(self):
        return [
            InstrumentPair(
                exchange=Exchange.BINANCE,
                base_asset="BTC",
                spot_symbol="BTCUSDT",
                perp_symbol="BTCUSDT",
                spot_price_increment=Decimal("0.01"),
                spot_quantity_increment=Decimal("0.00001"),
                spot_min_quantity=Decimal("0.0001"),
                spot_min_notional=Decimal("5"),
                perp_price_increment=Decimal("0.1"),
                perp_quantity_increment=Decimal("0.001"),
                perp_min_quantity=Decimal("0.001"),
                perp_min_notional=Decimal("5"),
                perp_contract_size=Decimal("1"),
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

    async def executable_quote(self, pair, quote):
        return quote.model_copy(
            update={
                "observed_at": datetime.now(UTC),
                "spot_ask_qty": Decimal("7"),
                "perp_bid_qty": Decimal("6"),
            }
        )

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


def test_history_snapshot_bucket_uses_hourly_intervals() -> None:
    value = datetime(2026, 7, 27, 14, 58, 41, 123456, tzinfo=UTC)
    assert history_snapshot_bucket(value) == datetime(
        2026,
        7,
        27,
        14,
        0,
        tzinfo=UTC,
    )
    assert ScannerSettings().retention_days == 7


def test_prioritizes_funding_history_by_projected_thirty_day_return() -> None:
    service = ScannerService(
        Database("sqlite+aiosqlite:///:memory:"),
        {Exchange.BINANCE: FakeAdapter()},
    )
    pairs = [
        InstrumentPair(
            exchange=Exchange.BINANCE,
            base_asset=base_asset,
            spot_symbol=f"{base_asset}USDT",
            perp_symbol=f"{base_asset}USDT",
        )
        for base_asset in ("LOW", "CURRENT", "PARTIAL", "UNKNOWN")
    ]
    service.pairs[Exchange.BINANCE] = pairs
    service.current["binance:CURRENT"] = FundingObservation(
        exchange=Exchange.BINANCE,
        base_asset="CURRENT",
        rate=Decimal("0.0004"),
        funding_at=datetime.now(UTC),
        interval_hours=Decimal("8"),
    )
    service.opportunities["binance:LOW"] = Opportunity.model_construct(
        net_return=Decimal("0.01"),
    )
    service.opportunities["binance:PARTIAL"] = Opportunity.model_construct(
        net_return=Decimal("0.02"),
    )

    prioritized = service._prioritized_history_pairs(Exchange.BINANCE)

    assert [pair.base_asset for pair in prioritized] == [
        "CURRENT",
        "PARTIAL",
        "LOW",
        "UNKNOWN",
    ]


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
    executable = await service.executable_opportunity(Exchange.BINANCE, "BTC")
    assert executable is not None
    assert executable.top_book_notional == Decimal("606")
    persisted = await database.latest_opportunities(
        exchanges={Exchange.BINANCE.value}
    )
    assert len(persisted) == 1
    assert persisted[0] == result
    pairs = await database.instrument_pairs(
        exchanges={Exchange.BINANCE.value}
    )
    assert len(pairs) == 1
    assert pairs[0].key == service.pairs[Exchange.BINANCE][0].key
    assert pairs[0].spot_symbol == "BTCUSDT"
    assert pairs[0].perp_contract_size == Decimal("1")
    assert pairs[0].perp_price_increment.quantize(
        Decimal("0.000000000001")
    ) == Decimal("0.100000000000")
    async with database.sessions() as session:
        instrument = await session.scalar(select(InstrumentRow))
        assert instrument is not None
        assert instrument.spot_quantity_increment == Decimal("0.00001")
        assert instrument.perp_contract_size == Decimal("1")
    await database.close()


async def test_reports_live_history_download_rate_and_warmup_progress() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = ScannerService(database, {Exchange.BINANCE: FakeAdapter()})
    await service.initialize()
    await service.refresh_catalog(Exchange.BINANCE)

    await service.refresh_history(Exchange.BINANCE)

    status = service.statuses[Exchange.BINANCE]
    assert status.instruments == 1
    assert status.history_ready == 1
    assert status.history_progress_percent == 100.0
    assert status.history_download_rate_per_minute is not None
    assert status.history_download_rate_per_minute > 0
    assert status.history_syncing is False

    previous_rate = status.history_download_rate_per_minute
    await service.refresh_history(Exchange.BINANCE)
    assert (
        service.statuses[Exchange.BINANCE].history_download_rate_per_minute
        == previous_rate
    )
    await database.close()
