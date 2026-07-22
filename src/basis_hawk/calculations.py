from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.models import (
    FeeRate,
    FundingObservation,
    InstrumentPair,
    MarketQuote,
    Opportunity,
    Quality,
)

DAYS_PER_YEAR = Decimal("365")
HOURS_PER_DAY = Decimal("24")


def annualize_current(rate: Decimal, interval_hours: Decimal) -> Decimal:
    if interval_hours <= 0:
        raise ValueError("funding interval must be positive")
    return rate * HOURS_PER_DAY / interval_hours * DAYS_PER_YEAR


def annualize_history(
    observations: list[FundingObservation], *, start: datetime, end: datetime
) -> Decimal | None:
    if end <= start:
        raise ValueError("history end must be after start")
    values = [item for item in observations if item.settled and start <= item.funding_at <= end]
    if not values:
        return None
    values.sort(key=lambda item: item.funding_at)
    observed_span = values[-1].funding_at - values[0].funding_at
    observed_span += timedelta(hours=float(values[0].interval_hours))
    covered_seconds = min((end - start).total_seconds(), observed_span.total_seconds())
    if covered_seconds <= 0:
        return None
    covered_days = Decimal(str(covered_seconds)) / Decimal("86400")
    return sum((item.rate for item in values), Decimal("0")) / covered_days * DAYS_PER_YEAR


def projected_net_return(apr_7d: Decimal, fee: FeeRate, holding_days: int) -> Decimal:
    funding_return = apr_7d * Decimal(holding_days) / DAYS_PER_YEAR
    round_trip_fees = Decimal("2") * (fee.spot_taker + fee.perp_taker)
    return funding_return - round_trip_fees


def build_opportunity(
    pair: InstrumentPair,
    quote: MarketQuote,
    current: FundingObservation,
    history: list[FundingObservation],
    fee: FeeRate,
    *,
    holding_days: int = 30,
    now: datetime | None = None,
) -> Opportunity:
    now = now or datetime.now(UTC)
    if quote.spot_ask <= 0 or quote.perp_bid <= 0:
        raise ValueError("quote prices must be positive")
    basis = quote.perp_bid / quote.spot_ask - Decimal("1")
    capacity = min(quote.spot_ask * quote.spot_ask_qty, quote.perp_bid * quote.perp_bid_qty)
    start_24h = now - timedelta(hours=24)
    start_7d = now - timedelta(days=7)
    apr_24h = annualize_history(history, start=start_24h, end=now)
    apr_7d = annualize_history(history, start=start_7d, end=now)
    coverage = [item.funding_at for item in history if item.settled and item.funding_at >= start_7d]
    warming = not coverage or max(coverage) - min(coverage) < timedelta(days=6)
    stale = now - quote.observed_at > timedelta(
        seconds=15
    ) or now - current.observed_at > timedelta(minutes=10)
    quality = Quality.STALE if stale else Quality.WARMING if warming else Quality.HEALTHY
    return Opportunity(
        exchange=pair.exchange,
        base_asset=pair.base_asset,
        spot_symbol=pair.spot_symbol,
        perp_symbol=pair.perp_symbol,
        observed_at=quote.observed_at,
        spot_ask=quote.spot_ask,
        perp_bid=quote.perp_bid,
        executable_basis=basis,
        top_book_notional=capacity,
        current_funding_rate=current.rate,
        funding_interval_hours=current.interval_hours,
        next_funding_at=current.next_funding_at,
        current_apr=annualize_current(current.rate, current.interval_hours),
        apr_24h=apr_24h,
        apr_7d=apr_7d,
        net_return=projected_net_return(apr_7d, fee, holding_days) if apr_7d is not None else None,
        spot_quote_volume_24h=quote.spot_quote_volume_24h,
        perp_quote_volume_24h=quote.perp_quote_volume_24h,
        spot_taker_fee=fee.spot_taker,
        perp_taker_fee=fee.perp_taker,
        quality=quality,
    )
