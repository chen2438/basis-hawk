from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from basis_hawk.models import Exchange, Opportunity, Quality


class AutoStrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: Literal["sandbox", "live"]
    enabled_exchanges: set[Exchange] = Field(min_length=1)
    leverage: int = Field(ge=1, le=10)
    notional_per_trade: Decimal = Field(gt=0)
    per_exchange_max_exposure: Decimal = Field(gt=0)
    global_max_exposure: Decimal = Field(gt=0)
    max_concurrent_positions: int = Field(ge=1, le=1000)
    minimum_current_apr: Decimal
    minimum_apr_24h: Decimal
    minimum_apr_7d: Decimal
    minimum_net_return: Decimal
    maximum_opening_basis: Decimal = Field(gt=Decimal("-1"), lt=Decimal("1"))
    minimum_two_leg_notional: Decimal = Field(gt=0)
    book_capacity_multiple: Decimal = Field(ge=1, le=100)
    normal_max_slippage: Decimal = Field(gt=0, le=Decimal("0.1"))
    emergency_max_slippage: Decimal = Field(gt=0, le=Decimal("0.25"))
    daily_max_loss: Decimal = Field(gt=0)
    minimum_reentry_minutes: int = Field(ge=0, le=10080)
    maximum_holding_hours: int = Field(ge=1, le=8760)
    minimum_liquidation_buffer: Decimal = Field(gt=0, le=Decimal("1"))
    close_funding_rate_below: Decimal
    close_net_return_below: Decimal
    close_basis_above: Decimal = Field(gt=Decimal("-1"), lt=Decimal("1"))
    take_profit_usdt: Decimal = Field(gt=0)
    stop_loss_usdt: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_portfolio_limits(self) -> AutoStrategyConfig:
        if self.per_exchange_max_exposure < self.notional_per_trade:
            raise ValueError(
                "per-exchange exposure must cover at least one trade"
            )
        if self.global_max_exposure < self.per_exchange_max_exposure:
            raise ValueError(
                "global exposure must be at least the per-exchange limit"
            )
        if self.minimum_two_leg_notional > self.notional_per_trade:
            raise ValueError(
                "minimum two-leg notional cannot exceed trade notional"
            )
        if self.emergency_max_slippage < self.normal_max_slippage:
            raise ValueError(
                "emergency slippage cannot be below normal slippage"
            )
        return self

    @field_serializer("*", when_used="json")
    def serialize_values(self, value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, set):
            return sorted(
                item.value if isinstance(item, Exchange) else str(item)
                for item in value
            )
        return value


class AutomationPosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    exchange: Exchange
    environment: str
    base_asset: str
    quantity: Decimal
    spot_entry_price: Decimal
    perp_entry_price: Decimal
    remaining_opening_fees_usdt: Decimal
    status: str
    opened_at: datetime
    closed_at: datetime | None = None
    liquidation_buffer: Decimal | None = None


class AutomaticDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["open", "close"]
    opportunity: Opportunity
    position_id: str | None = None
    reason: str
    estimated_net_pnl_usdt: Decimal | None = None


class AutomaticEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: AutomaticDecision | None = None
    opening_block_reason: str | None = None


def evaluate_automatic_strategy(
    *,
    config: AutoStrategyConfig,
    opportunities: list[Opportunity],
    positions: list[AutomationPosition],
    daily_realized_pnl: Decimal,
    now: datetime | None = None,
) -> AutomaticEvaluation:
    observed_now = _utc(now or datetime.now(UTC))
    enabled = {item.value for item in config.enabled_exchanges}
    opportunity_by_key = {
        item.key: item
        for item in opportunities
        if item.exchange.value in enabled
    }
    scoped_positions = [
        item
        for item in positions
        if item.environment == config.environment
        and item.exchange.value in enabled
    ]

    close_candidates: list[
        tuple[int, Decimal, datetime, AutomaticDecision]
    ] = []
    for position in scoped_positions:
        if position.status != "open" or position.quantity <= 0:
            continue
        opportunity = opportunity_by_key.get(
            f"{position.exchange.value}:{position.base_asset}"
        )
        if opportunity is None or not _tradable_quote(
            opportunity,
            observed_now,
        ):
            continue
        required_capacity = max(
            position.quantity * opportunity.spot_bid,
            position.quantity * opportunity.perp_ask,
        )
        if required_capacity > opportunity.close_top_book_notional:
            continue
        closing_fees = (
            position.quantity
            * opportunity.spot_bid
            * opportunity.spot_taker_fee
            + position.quantity
            * opportunity.perp_ask
            * opportunity.perp_taker_fee
        )
        estimated_net_pnl = (
            (
                opportunity.spot_bid
                - position.spot_entry_price
                + position.perp_entry_price
                - opportunity.perp_ask
            )
            * position.quantity
            - position.remaining_opening_fees_usdt
            - closing_fees
        )
        close_basis = (
            opportunity.perp_ask - opportunity.spot_bid
        ) / opportunity.spot_bid
        trigger: tuple[int, str] | None = None
        if (
            position.liquidation_buffer is not None
            and position.liquidation_buffer
            < config.minimum_liquidation_buffer
        ):
            trigger = (0, "liquidation buffer is below the configured minimum")
        elif estimated_net_pnl <= -config.stop_loss_usdt:
            trigger = (1, "estimated net PnL reached the stop loss")
        elif observed_now - _utc(position.opened_at) >= timedelta(
            hours=config.maximum_holding_hours
        ):
            trigger = (2, "maximum holding period was reached")
        elif (
            opportunity.current_funding_rate
            < config.close_funding_rate_below
        ):
            trigger = (3, "current funding rate crossed the close threshold")
        elif (
            opportunity.net_return is not None
            and opportunity.net_return < config.close_net_return_below
        ):
            trigger = (4, "estimated net return crossed the close threshold")
        elif close_basis > config.close_basis_above:
            trigger = (5, "executable closing basis crossed the close threshold")
        elif estimated_net_pnl >= config.take_profit_usdt:
            trigger = (6, "estimated net PnL reached the take profit")
        if trigger is None:
            continue
        decision = AutomaticDecision(
            action="close",
            opportunity=opportunity,
            position_id=position.id,
            reason=trigger[1],
            estimated_net_pnl_usdt=estimated_net_pnl,
        )
        close_candidates.append(
            (
                trigger[0],
                estimated_net_pnl,
                _utc(position.opened_at),
                decision,
            )
        )
    if close_candidates:
        close_candidates.sort(key=lambda item: item[:3])
        return AutomaticEvaluation(decision=close_candidates[0][3])

    opening_block_reason = _opening_portfolio_block(
        config=config,
        positions=scoped_positions,
        daily_realized_pnl=daily_realized_pnl,
    )
    if opening_block_reason is not None:
        return AutomaticEvaluation(
            opening_block_reason=opening_block_reason,
        )

    exposure_by_exchange: dict[Exchange, Decimal] = {}
    global_exposure = Decimal("0")
    active_keys: set[str] = set()
    last_closed_by_key: dict[str, datetime] = {}
    for position in scoped_positions:
        key = f"{position.exchange.value}:{position.base_asset}"
        if position.status in {"open", "closing"} and position.quantity > 0:
            exposure = position.quantity * position.spot_entry_price
            exposure_by_exchange[position.exchange] = (
                exposure_by_exchange.get(position.exchange, Decimal("0"))
                + exposure
            )
            global_exposure += exposure
            active_keys.add(key)
        elif position.closed_at is not None:
            closed_at = _utc(position.closed_at)
            last_closed_by_key[key] = max(
                last_closed_by_key.get(key, closed_at),
                closed_at,
            )

    candidates: list[Opportunity] = []
    for opportunity in opportunities:
        if (
            opportunity.exchange not in config.enabled_exchanges
            or opportunity.key in active_keys
            or not _tradable_quote(opportunity, observed_now)
            or opportunity.apr_24h is None
            or opportunity.apr_7d is None
            or opportunity.net_return is None
            or opportunity.current_apr < config.minimum_current_apr
            or opportunity.apr_24h < config.minimum_apr_24h
            or opportunity.apr_7d < config.minimum_apr_7d
            or opportunity.net_return < config.minimum_net_return
            or opportunity.executable_basis > config.maximum_opening_basis
            or opportunity.top_book_notional
            < config.notional_per_trade * config.book_capacity_multiple
            or opportunity.top_book_notional
            < config.minimum_two_leg_notional
        ):
            continue
        last_closed = last_closed_by_key.get(opportunity.key)
        if (
            last_closed is not None
            and observed_now - last_closed
            < timedelta(minutes=config.minimum_reentry_minutes)
        ):
            continue
        exchange_exposure = exposure_by_exchange.get(
            opportunity.exchange,
            Decimal("0"),
        )
        if (
            exchange_exposure + config.notional_per_trade
            > config.per_exchange_max_exposure
            or global_exposure + config.notional_per_trade
            > config.global_max_exposure
        ):
            continue
        candidates.append(opportunity)
    if not candidates:
        return AutomaticEvaluation(
            opening_block_reason="no opportunity satisfies every opening rule",
        )
    candidates.sort(
        key=lambda item: (
            -(item.net_return or Decimal("-Infinity")),
            -item.current_apr,
            item.exchange.value,
            item.base_asset,
        )
    )
    return AutomaticEvaluation(
        decision=AutomaticDecision(
            action="open",
            opportunity=candidates[0],
            reason="highest-ranked opportunity satisfies every opening rule",
        )
    )


def _opening_portfolio_block(
    *,
    config: AutoStrategyConfig,
    positions: list[AutomationPosition],
    daily_realized_pnl: Decimal,
) -> str | None:
    if daily_realized_pnl <= -config.daily_max_loss:
        return "daily realized loss limit was reached"
    active_count = sum(
        item.status in {"open", "closing"} and item.quantity > 0
        for item in positions
    )
    if active_count >= config.max_concurrent_positions:
        return "maximum concurrent positions was reached"
    return None


def _tradable_quote(opportunity: Opportunity, now: datetime) -> bool:
    if opportunity.quality != Quality.HEALTHY:
        return False
    observed_at = _utc(opportunity.observed_at)
    return (
        observed_at <= now + timedelta(seconds=5)
        and now - observed_at <= timedelta(seconds=15)
        and opportunity.spot_bid > 0
        and opportunity.spot_ask > 0
        and opportunity.perp_bid > 0
        and opportunity.perp_ask > 0
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
