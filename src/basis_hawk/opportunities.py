from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from basis_hawk.credentials import (
    AccountFeeSchedule,
    ExchangeEnvironment,
)
from basis_hawk.models import Opportunity, Quality
from basis_hawk.multi_leg import DecimalPayload
from basis_hawk.storage import Database, ExchangeCredentialRow


class OpportunityType(StrEnum):
    FUNDING = "funding"
    CROSS_FUNDING = "cross_funding"
    BASIS = "basis"


class OpportunityLegView(DecimalPayload):
    account_id: str | None
    exchange: str
    market_type: str
    side: str
    symbol: str
    price: Decimal
    quote_volume_24h: Decimal
    funding_rate: Decimal | None
    funding_interval_hours: Decimal | None
    annualized_funding: Decimal | None
    fee_rate: Decimal
    fee_source: str


class ArbitrageOpportunityView(DecimalPayload):
    id: str
    opportunity_type: OpportunityType
    base_asset: str
    observed_at: datetime
    entry_spread: Decimal
    annualized_return: Decimal
    projected_return: Decimal
    estimated_round_trip_fees: Decimal
    holding_days: int = Field(ge=1, le=365)
    executable_notional_usdt: Decimal
    legs: list[OpportunityLegView]


class OpportunityService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list(
        self,
        *,
        opportunity_type: OpportunityType,
        exchanges: set[str] | None = None,
        search: str | None = None,
        minimum_annualized_return: Decimal = Decimal("0"),
        holding_days: int = 7,
        maximum_age_seconds: int = 30,
        limit: int = 100,
    ) -> list[ArbitrageOpportunityView]:
        rows = await self.database.latest_opportunities(exchanges=exchanges)
        fees = await self._venue_fees(rows)
        cutoff = datetime.now(UTC) - timedelta(seconds=maximum_age_seconds)
        usable = [
            item
            for item in rows
            if item.quality == Quality.HEALTHY
            and _utc(item.observed_at) >= cutoff
            and (
                search is None
                or search.strip().upper() in item.base_asset.upper()
            )
        ]
        if opportunity_type == OpportunityType.CROSS_FUNDING:
            candidates = _cross_funding(usable, holding_days, fees)
        else:
            candidates = _spot_perpetual(
                usable,
                holding_days,
                opportunity_type,
                fees,
            )
        def ranking(
            value: ArbitrageOpportunityView,
        ) -> tuple[Decimal, Decimal, str, str]:
            primary = (
                value.projected_return
                if opportunity_type == OpportunityType.BASIS
                else value.annualized_return
            )
            return (
                -primary,
                -(
                    value.annualized_return
                    if opportunity_type == OpportunityType.BASIS
                    else value.projected_return
                ),
                value.base_asset,
                value.id,
            )

        return [
            item
            for item in sorted(
                candidates,
                key=ranking,
            )
            if item.annualized_return >= minimum_annualized_return
        ][:limit]

    async def _venue_fees(
        self,
        rows: list[Opportunity],
    ) -> dict[str, VenueFees]:
        defaults = {
            item.exchange.value: VenueFees(
                account_id=None,
                spot_taker=item.spot_taker_fee,
                spot_source="default",
                perpetual_taker=item.perp_taker_fee,
                perpetual_source="default",
            )
            for item in rows
        }
        accounts = await self.database.list_exchange_credentials()
        for exchange, account in _default_live_accounts(accounts).items():
            fallback = defaults.get(exchange)
            if fallback is None:
                continue
            try:
                schedule = AccountFeeSchedule.model_validate_json(
                    account.fee_payload or "{}"
                )
            except ValueError:
                schedule = AccountFeeSchedule()
            defaults[exchange] = VenueFees(
                account_id=account.id,
                spot_taker=(
                    schedule.spot_taker
                    if schedule.spot_taker is not None
                    else fallback.spot_taker
                ),
                spot_source=(
                    schedule.source
                    if schedule.spot_taker is not None
                    else "default"
                ),
                perpetual_taker=(
                    schedule.perpetual_taker
                    if schedule.perpetual_taker is not None
                    else fallback.perpetual_taker
                ),
                perpetual_source=(
                    schedule.source
                    if schedule.perpetual_taker is not None
                    else "default"
                ),
            )
        return defaults


@dataclass(frozen=True)
class VenueFees:
    account_id: str | None
    spot_taker: Decimal
    spot_source: str
    perpetual_taker: Decimal
    perpetual_source: str


def _spot_perpetual(
    rows: list[Opportunity],
    holding_days: int,
    opportunity_type: OpportunityType,
    fees_by_exchange: dict[str, VenueFees],
) -> list[ArbitrageOpportunityView]:
    by_base: dict[str, list[Opportunity]] = defaultdict(list)
    for item in rows:
        by_base[item.base_asset].append(item)
    result: list[ArbitrageOpportunityView] = []
    for base_asset, venues in by_base.items():
        for spot in venues:
            if spot.spot_ask <= 0:
                continue
            for perpetual in venues:
                if perpetual.perp_bid <= 0:
                    continue
                entry_spread = perpetual.perp_bid / spot.spot_ask - 1
                funding_per_day = (
                    perpetual.current_funding_rate
                    * Decimal("24")
                    / perpetual.funding_interval_hours
                )
                annualized = funding_per_day * Decimal("365")
                spot_fees = fees_by_exchange[spot.exchange.value]
                perpetual_fees = fees_by_exchange[
                    perpetual.exchange.value
                ]
                fees = Decimal("2") * (
                    spot_fees.spot_taker
                    + perpetual_fees.perpetual_taker
                )
                projected = (
                    entry_spread
                    + funding_per_day * Decimal(holding_days)
                    - fees
                )
                result.append(
                    ArbitrageOpportunityView(
                        id=_id(
                            opportunity_type,
                            base_asset,
                            spot.exchange.value,
                            perpetual.exchange.value,
                        ),
                        opportunity_type=opportunity_type,
                        base_asset=base_asset,
                        observed_at=min(
                            _utc(spot.observed_at),
                            _utc(perpetual.observed_at),
                        ),
                        entry_spread=entry_spread,
                        annualized_return=annualized,
                        projected_return=projected,
                        estimated_round_trip_fees=fees,
                        holding_days=holding_days,
                        executable_notional_usdt=min(
                            spot.spot_ask_notional,
                            perpetual.perp_bid_notional,
                        ),
                        legs=[
                            _spot_leg(spot, spot_fees),
                            _short_perpetual_leg(
                                perpetual,
                                perpetual_fees,
                            ),
                        ],
                    )
                )
    return result


def _cross_funding(
    rows: list[Opportunity],
    holding_days: int,
    fees_by_exchange: dict[str, VenueFees],
) -> list[ArbitrageOpportunityView]:
    by_base: dict[str, list[Opportunity]] = defaultdict(list)
    for item in rows:
        by_base[item.base_asset].append(item)
    result: list[ArbitrageOpportunityView] = []
    for base_asset, venues in by_base.items():
        for short in venues:
            if short.perp_bid <= 0:
                continue
            short_daily = (
                short.current_funding_rate
                * Decimal("24")
                / short.funding_interval_hours
            )
            for long in venues:
                if long.exchange == short.exchange or long.perp_ask <= 0:
                    continue
                long_daily = (
                    long.current_funding_rate
                    * Decimal("24")
                    / long.funding_interval_hours
                )
                daily_carry = short_daily - long_daily
                if daily_carry <= 0:
                    continue
                entry_spread = short.perp_bid / long.perp_ask - 1
                annualized = daily_carry * Decimal("365")
                short_fees = fees_by_exchange[short.exchange.value]
                long_fees = fees_by_exchange[long.exchange.value]
                fees = Decimal("2") * (
                    short_fees.perpetual_taker
                    + long_fees.perpetual_taker
                )
                projected = (
                    entry_spread
                    + daily_carry * Decimal(holding_days)
                    - fees
                )
                result.append(
                    ArbitrageOpportunityView(
                        id=_id(
                            OpportunityType.CROSS_FUNDING,
                            base_asset,
                            short.exchange.value,
                            long.exchange.value,
                        ),
                        opportunity_type=OpportunityType.CROSS_FUNDING,
                        base_asset=base_asset,
                        observed_at=min(
                            _utc(short.observed_at),
                            _utc(long.observed_at),
                        ),
                        entry_spread=entry_spread,
                        annualized_return=annualized,
                        projected_return=projected,
                        estimated_round_trip_fees=fees,
                        holding_days=holding_days,
                        executable_notional_usdt=min(
                            short.perp_bid_notional,
                            long.close_top_book_notional,
                        ),
                        legs=[
                            _short_perpetual_leg(short, short_fees),
                            _long_perpetual_leg(long, long_fees),
                        ],
                    )
                )
    return result


def _spot_leg(
    item: Opportunity,
    fees: VenueFees,
) -> OpportunityLegView:
    return OpportunityLegView(
        account_id=fees.account_id,
        exchange=item.exchange.value,
        market_type="spot",
        side="buy",
        symbol=item.spot_symbol,
        price=item.spot_ask,
        quote_volume_24h=item.spot_quote_volume_24h,
        funding_rate=None,
        funding_interval_hours=None,
        annualized_funding=None,
        fee_rate=fees.spot_taker,
        fee_source=fees.spot_source,
    )


def _short_perpetual_leg(
    item: Opportunity,
    fees: VenueFees,
) -> OpportunityLegView:
    return OpportunityLegView(
        account_id=fees.account_id,
        exchange=item.exchange.value,
        market_type="perpetual",
        side="sell",
        symbol=item.perp_symbol,
        price=item.perp_bid,
        quote_volume_24h=item.perp_quote_volume_24h,
        funding_rate=item.current_funding_rate,
        funding_interval_hours=item.funding_interval_hours,
        annualized_funding=item.current_apr,
        fee_rate=fees.perpetual_taker,
        fee_source=fees.perpetual_source,
    )


def _long_perpetual_leg(
    item: Opportunity,
    fees: VenueFees,
) -> OpportunityLegView:
    return OpportunityLegView(
        account_id=fees.account_id,
        exchange=item.exchange.value,
        market_type="perpetual",
        side="buy",
        symbol=item.perp_symbol,
        price=item.perp_ask,
        quote_volume_24h=item.perp_quote_volume_24h,
        funding_rate=item.current_funding_rate,
        funding_interval_hours=item.funding_interval_hours,
        annualized_funding=item.current_apr,
        fee_rate=fees.perpetual_taker,
        fee_source=fees.perpetual_source,
    )


def _default_live_accounts(
    accounts: list[ExchangeCredentialRow],
) -> dict[str, ExchangeCredentialRow]:
    result: dict[str, ExchangeCredentialRow] = {}
    for item in accounts:
        if (
            item.environment == ExchangeEnvironment.LIVE.value
            and item.is_default
        ):
            result[item.exchange] = item
    return result


def _id(
    opportunity_type: OpportunityType,
    base_asset: str,
    first_exchange: str,
    second_exchange: str,
) -> str:
    payload = (
        f"{opportunity_type.value}:{base_asset}:"
        f"{first_exchange}:{second_exchange}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
