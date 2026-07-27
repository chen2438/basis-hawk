from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange, Opportunity, Quality
from basis_hawk.storage import Database
from basis_hawk.trading import (
    IdempotencyConflict,
    TradeLedger,
    TradeValidationError,
)


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
    notional_usdt: Decimal | None = None
    estimated_net_pnl_usdt: Decimal | None = None


class AutomaticEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: AutomaticDecision | None = None
    opening_block_reason: str | None = None


class AutomaticTradingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluated: bool
    created: bool
    intent_id: str | None = None
    action: Literal["open", "close"] | None = None
    reason: str


def evaluate_automatic_strategy(
    *,
    config: AutoStrategyConfig,
    opportunities: list[Opportunity],
    positions: list[AutomationPosition],
    daily_realized_pnl: Decimal,
    blocked_open_keys: set[str] | None = None,
    now: datetime | None = None,
) -> AutomaticEvaluation:
    observed_now = _utc(now or datetime.now(UTC))
    enabled = {item.value for item in config.enabled_exchanges}
    opportunity_by_key = {
        item.key: item
        for item in opportunities
        if item.exchange.value in enabled
    }
    blocked_keys = blocked_open_keys or set()
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

    candidates: list[tuple[Opportunity, Decimal]] = []
    for opportunity in opportunities:
        if (
            opportunity.exchange not in config.enabled_exchanges
            or opportunity.key in blocked_keys
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
        notional = min(
            config.notional_per_trade,
            opportunity.top_book_notional / config.book_capacity_multiple,
            config.per_exchange_max_exposure - exchange_exposure,
            config.global_max_exposure - global_exposure,
        )
        if notional < config.minimum_two_leg_notional:
            continue
        candidates.append((opportunity, notional))
    if not candidates:
        return AutomaticEvaluation(
            opening_block_reason="no opportunity satisfies every opening rule",
        )
    candidates.sort(
        key=lambda item: (
            -(item[0].net_return or Decimal("-Infinity")),
            -item[0].current_apr,
            item[0].exchange.value,
            item[0].base_asset,
        )
    )
    opportunity, notional = candidates[0]
    return AutomaticEvaluation(
        decision=AutomaticDecision(
            action="open",
            opportunity=opportunity,
            reason=(
                "highest-ranked opportunity satisfies every opening rule "
                "at the available bounded notional"
            ),
            notional_usdt=notional,
        )
    )


class AutomaticTradingService:
    def __init__(
        self,
        database: Database,
        *,
        ledger: TradeLedger | None = None,
    ) -> None:
        self.database = database
        self.ledger = ledger or TradeLedger(database)

    async def run_once(self) -> AutomaticTradingResult:
        control = await self.database.automation_control()
        if control.state != "enabled" or control.active_strategy_id is None:
            return AutomaticTradingResult(
                evaluated=False,
                created=False,
                reason="automatic trading is not enabled",
            )
        execution = await self.database.execution_control()
        if execution is None or execution.state != "ready":
            return AutomaticTradingResult(
                evaluated=False,
                created=False,
                reason="account execution is not ready",
            )
        strategy = await self.database.strategy_version(
            control.active_strategy_id
        )
        if strategy is None:
            return AutomaticTradingResult(
                evaluated=False,
                created=False,
                reason="active strategy version was not found",
            )
        config = AutoStrategyConfig.model_validate(
            json.loads(strategy.payload)
        )
        now = datetime.now(UTC)
        exchanges = {item.value for item in config.enabled_exchanges}
        opportunities = await self.database.latest_opportunities(
            exchanges=exchanges
        )
        pair_by_key = {
            item.key: item
            for item in await self.database.instrument_pairs(
                exchanges=exchanges
            )
        }
        rows = await self.database.list_paired_positions()
        liquidation_buffers = (
            await self.database.position_liquidation_buffers(
                environment=config.environment,
                exchanges=exchanges,
            )
        )
        positions = [
            AutomationPosition(
                id=item.id,
                exchange=Exchange(item.exchange),
                environment=item.environment,
                base_asset=item.base_asset,
                quantity=item.quantity,
                spot_entry_price=item.spot_entry_price,
                perp_entry_price=item.perp_entry_price,
                remaining_opening_fees_usdt=(
                    item.remaining_opening_fees_usdt
                ),
                status=item.status,
                opened_at=item.opened_at,
                closed_at=item.closed_at,
                liquidation_buffer=liquidation_buffers.get(item.id),
            )
            for item in rows
        ]
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_pnl = await self.database.daily_realized_pnl(
            environment=config.environment,
            exchanges=exchanges,
            since=day_start,
        )
        evaluation = evaluate_automatic_strategy(
            config=config,
            opportunities=opportunities,
            positions=positions,
            daily_realized_pnl=daily_pnl,
            blocked_open_keys=await self.database.active_open_intent_keys(
                environment=config.environment
            ),
            now=now,
        )
        if evaluation.decision is None:
            if evaluation.opening_block_reason == (
                "daily realized loss limit was reached"
            ):
                await self.database.set_automation_control(
                    state="paused",
                    active_strategy_id=strategy.id,
                    reason=evaluation.opening_block_reason,
                    actor="system",
                )
                await self.database.append_audit(
                    "automation.daily_loss_paused",
                    actor="system",
                    details={
                        "strategy_id": strategy.id,
                        "daily_realized_pnl": format(daily_pnl, "f"),
                    },
                )
            return AutomaticTradingResult(
                evaluated=True,
                created=False,
                reason=(
                    evaluation.opening_block_reason
                    or "no automatic action is required"
                ),
            )
        decision = evaluation.decision
        pair = pair_by_key.get(decision.opportunity.key)
        if pair is None or not pair.trading_rules_complete:
            return AutomaticTradingResult(
                evaluated=True,
                created=False,
                action=decision.action,
                reason="current instrument trading rules are incomplete",
            )
        idempotency_key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join(
                (
                    "basis-hawk",
                    strategy.id,
                    decision.action,
                    decision.position_id or decision.opportunity.key,
                    decision.opportunity.observed_at.isoformat(),
                )
            ),
        )
        settings = await self.database.load_settings()
        environment = ExchangeEnvironment(config.environment)
        try:
            if decision.action == "open":
                if decision.notional_usdt is None:
                    raise RuntimeError(
                        "automatic open decision has no bounded notional"
                    )
                intent, created = await self.ledger.plan_live_open(
                    opportunity=decision.opportunity,
                    pair=pair,
                    notional_usdt=decision.notional_usdt,
                    idempotency_key=idempotency_key,
                    settings=settings,
                    environment=environment,
                    leverage=config.leverage,
                    maximum_slippage=config.normal_max_slippage,
                    now=now,
                )
            else:
                if decision.position_id is None:
                    raise RuntimeError(
                        "automatic close decision has no paired position"
                    )
                intent, created = await self.ledger.plan_live_close(
                    position_id=decision.position_id,
                    opportunity=decision.opportunity,
                    pair=pair,
                    idempotency_key=idempotency_key,
                    settings=settings,
                    environment=environment,
                    maximum_slippage=config.normal_max_slippage,
                    now=now,
                )
        except (IdempotencyConflict, TradeValidationError) as exc:
            return AutomaticTradingResult(
                evaluated=True,
                created=False,
                action=decision.action,
                reason=str(exc),
            )
        if created:
            await self.database.append_audit(
                f"automation.{decision.action}_planned",
                actor="system",
                details={
                    "strategy_id": strategy.id,
                    "intent_id": intent.id,
                    "exchange": decision.opportunity.exchange.value,
                    "base_asset": decision.opportunity.base_asset,
                    "reason": decision.reason,
                },
            )
        return AutomaticTradingResult(
            evaluated=True,
            created=created,
            intent_id=intent.id,
            action=decision.action,
            reason=decision.reason,
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
