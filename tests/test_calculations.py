from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.calculations import annualize_current, build_opportunity, projected_net_return
from basis_hawk.models import (
    Exchange,
    FeeRate,
    FundingObservation,
    InstrumentPair,
    MarketQuote,
    Quality,
)


def test_annualizes_dynamic_interval() -> None:
    assert annualize_current(Decimal("0.0001"), Decimal("8")) == Decimal("0.1095")
    assert annualize_current(Decimal("-0.0001"), Decimal("4")) == Decimal("-0.2190")


def test_projects_round_trip_fees() -> None:
    fee = FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.0005"))
    assert projected_net_return(Decimal("0.365"), fee, 30) == Decimal("0.027")


def test_builds_executable_opportunity() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    pair = InstrumentPair(
        exchange=Exchange.BINANCE,
        base_asset="BTC",
        spot_symbol="BTCUSDT",
        perp_symbol="BTCUSDT",
    )
    quote = MarketQuote(
        exchange=Exchange.BINANCE,
        base_asset="BTC",
        observed_at=now,
        spot_bid=Decimal("99"),
        spot_bid_qty=Decimal("3"),
        spot_ask=Decimal("100"),
        spot_ask_qty=Decimal("2"),
        perp_bid=Decimal("101"),
        perp_bid_qty=Decimal("1"),
        perp_ask=Decimal("102"),
        perp_ask_qty=Decimal("2"),
        spot_quote_volume_24h=Decimal("2000000"),
        perp_quote_volume_24h=Decimal("3000000"),
    )
    history = [
        FundingObservation(
            exchange=Exchange.BINANCE,
            base_asset="BTC",
            rate=Decimal("0.0001"),
            funding_at=now - timedelta(hours=8 * offset),
            observed_at=now,
            settled=True,
        )
        for offset in range(22)
    ]
    current = FundingObservation(
        exchange=Exchange.BINANCE,
        base_asset="BTC",
        rate=Decimal("0.0002"),
        funding_at=now,
        observed_at=now,
    )
    result = build_opportunity(
        pair,
        quote,
        current,
        history,
        FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.0005")),
        now=now,
    )
    assert result.executable_basis == Decimal("0.01")
    assert result.spot_ask_notional == Decimal("200")
    assert result.perp_bid_notional == Decimal("101")
    assert result.top_book_notional == Decimal("101")
    assert result.close_top_book_notional == Decimal("204")
    assert result.quality == Quality.HEALTHY
