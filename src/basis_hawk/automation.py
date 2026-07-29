from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from basis_hawk.calculations import (
    apply_current_funding_fallback,
    build_opportunity,
    build_sandbox_opportunity,
)
from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.exchanges import ExchangeAdapter, GateAdapter
from basis_hawk.executable_quotes import (
    market_quote_from_opportunity,
    opportunity_with_executable_quote,
)
from basis_hawk.gate_price_guard import GatePerpPriceGuard, PerpPriceGuard
from basis_hawk.models import (
    Exchange,
    FeeRate,
    FundingObservation,
    InstrumentPair,
    Opportunity,
    Quality,
)
from basis_hawk.sizing import protective_limit_price
from basis_hawk.storage import Database
from basis_hawk.trading import (
    IdempotencyConflict,
    TradeLedger,
    TradeValidationError,
)

logger = logging.getLogger(__name__)
GATE_AUTOMATIC_DEPTH_CANDIDATES = 20


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
    minimum_opening_basis: Decimal = Field(
        default=Decimal("-0.999999999999"),
        gt=Decimal("-1"),
        lt=Decimal("1"),
    )
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
        if self.minimum_opening_basis > self.maximum_opening_basis:
            raise ValueError(
                "minimum opening basis cannot exceed maximum opening basis"
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
    for position in scoped_positions:
        if position.status in {"open", "closing"} and position.quantity > 0:
            exposure = position.quantity * position.spot_entry_price
            exposure_by_exchange[position.exchange] = (
                exposure_by_exchange.get(position.exchange, Decimal("0"))
                + exposure
            )
            global_exposure += exposure

    candidates: list[tuple[Opportunity, Decimal]] = []
    for opportunity in _opening_rule_candidates(
        config=config,
        opportunities=opportunities,
        positions=scoped_positions,
        blocked_open_keys=blocked_keys,
        now=observed_now,
    ):
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


def _opening_rule_candidates(
    *,
    config: AutoStrategyConfig,
    opportunities: list[Opportunity],
    positions: list[AutomationPosition],
    blocked_open_keys: set[str],
    now: datetime,
) -> list[Opportunity]:
    active_keys: set[str] = set()
    last_closed_by_key: dict[str, datetime] = {}
    for position in positions:
        if (
            position.environment != config.environment
            or position.exchange not in config.enabled_exchanges
        ):
            continue
        key = f"{position.exchange.value}:{position.base_asset}"
        if position.status in {"open", "closing"} and position.quantity > 0:
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
            or opportunity.key in blocked_open_keys
            or opportunity.key in active_keys
            or not _tradable_quote(opportunity, now)
            or opportunity.apr_24h is None
            or opportunity.apr_7d is None
            or opportunity.net_return is None
            or opportunity.current_apr < config.minimum_current_apr
            or opportunity.apr_24h < config.minimum_apr_24h
            or opportunity.apr_7d < config.minimum_apr_7d
            or opportunity.net_return < config.minimum_net_return
            or opportunity.executable_basis < config.minimum_opening_basis
            or opportunity.executable_basis > config.maximum_opening_basis
        ):
            continue
        last_closed = last_closed_by_key.get(opportunity.key)
        if (
            last_closed is not None
            and now - last_closed
            < timedelta(minutes=config.minimum_reentry_minutes)
        ):
            continue
        candidates.append(opportunity)
    candidates.sort(
        key=lambda item: (
            -(item.net_return or Decimal("-Infinity")),
            -item.current_apr,
            item.exchange.value,
            item.base_asset,
        )
    )
    return candidates


class AutomaticTradingService:
    def __init__(
        self,
        database: Database,
        *,
        ledger: TradeLedger | None = None,
        gate_adapter_factory: Callable[
            [ExchangeEnvironment],
            ExchangeAdapter,
        ]
        | None = None,
        gate_price_guard_factory: Callable[
            [ExchangeEnvironment],
            PerpPriceGuard,
        ]
        | None = None,
    ) -> None:
        self.database = database
        self.ledger = ledger or TradeLedger(database)
        self.gate_adapter_factory = gate_adapter_factory or (
            lambda environment: GateAdapter(environment=environment)
        )
        self.gate_price_guard_factory = gate_price_guard_factory or (
            lambda environment: GatePerpPriceGuard(environment)
        )

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
        blocked_open_keys = await self.database.active_open_intent_keys(
            environment=config.environment
        )
        if Exchange.GATE in config.enabled_exchanges:
            opportunities = await self._refresh_gate_automatic_capacity(
                config=config,
                opportunities=opportunities,
                positions=positions,
                blocked_open_keys=blocked_open_keys,
                pair_by_key=pair_by_key,
                now=now,
            )
        evaluation = evaluate_automatic_strategy(
            config=config,
            opportunities=opportunities,
            positions=positions,
            daily_realized_pnl=daily_pnl,
            blocked_open_keys=blocked_open_keys,
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
        if (
            decision.opportunity.exchange == Exchange.GATE
            and config.environment == ExchangeEnvironment.SANDBOX.value
            and not await self._gate_sandbox_decision_is_executable(
                decision=decision,
                pair=pair,
                maximum_slippage=config.normal_max_slippage,
            )
        ):
            return AutomaticTradingResult(
                evaluated=True,
                created=False,
                action=decision.action,
                reason=(
                    "Gate Sandbox perpetual top book is outside the "
                    "exchange price-protection band"
                ),
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

    async def _gate_sandbox_decision_is_executable(
        self,
        *,
        decision: AutomaticDecision,
        pair: InstrumentPair,
        maximum_slippage: Decimal,
    ) -> bool:
        side = "sell" if decision.action == "open" else "buy"
        reference_price = (
            decision.opportunity.perp_bid
            if side == "sell"
            else decision.opportunity.perp_ask
        )
        planned_limit_price = protective_limit_price(
            reference_price=reference_price,
            maximum_slippage=maximum_slippage,
            side=side,
            price_increment=pair.perp_price_increment,
        )
        guard = self.gate_price_guard_factory(
            ExchangeEnvironment.SANDBOX
        )
        try:
            return (
                await guard.executable_limit(
                    symbol=pair.perp_symbol,
                    side=side,
                    planned_limit_price=planned_limit_price,
                )
                is not None
            )
        except Exception:
            return False
        finally:
            try:
                await guard.close()
            except Exception:
                pass

    async def _refresh_gate_automatic_capacity(
        self,
        *,
        config: AutoStrategyConfig,
        opportunities: list[Opportunity],
        positions: list[AutomationPosition],
        blocked_open_keys: set[str],
        pair_by_key: dict[str, InstrumentPair],
        now: datetime,
    ) -> list[Opportunity]:
        adapter = self.gate_adapter_factory(
            ExchangeEnvironment(config.environment)
        )
        depth_pair_by_key = pair_by_key
        sandbox_funding_by_key: dict[str, FundingObservation] = {}
        sandbox_fee: FeeRate | None = None
        sandbox_holding_days = 30
        if config.environment == ExchangeEnvironment.SANDBOX.value:
            try:
                sandbox_pairs = await adapter.instruments()
                sandbox_quotes, sandbox_funding = await asyncio.gather(
                    adapter.quotes(sandbox_pairs),
                    adapter.current_funding(sandbox_pairs),
                )
            except (RuntimeError, ValueError):
                await adapter.close()
                logger.info(
                    "automatic Gate sandbox catalog unavailable",
                    extra={
                        "exchange": Exchange.GATE.value,
                        "environment": config.environment,
                    },
                )
                return opportunities
            depth_pair_by_key = {
                pair.key: pair
                for pair in sandbox_pairs
            }
            pair_by_key.update(depth_pair_by_key)
            sandbox_funding_by_key = {
                f"{item.exchange.value}:{item.base_asset}": item
                for item in sandbox_funding
            }
            quote_by_key = {
                f"{item.exchange.value}:{item.base_asset}": item
                for item in sandbox_quotes
            }
            settings = await self.database.load_settings()
            sandbox_fee = settings.fees[Exchange.GATE]
            sandbox_holding_days = settings.holding_period_days
            sandbox_opportunities = []
            for key, pair in depth_pair_by_key.items():
                quote = quote_by_key.get(key)
                funding = sandbox_funding_by_key.get(key)
                if quote is None or funding is None:
                    continue
                try:
                    sandbox_opportunities.append(
                        build_sandbox_opportunity(
                            pair=pair,
                            quote=quote,
                            current=funding,
                            fee=sandbox_fee,
                            holding_days=sandbox_holding_days,
                            now=now,
                        )
                    )
                except ValueError:
                    continue
            opportunities = [
                item
                for item in opportunities
                if item.exchange != Exchange.GATE
            ] + sandbox_opportunities
        opportunity_by_key = {
            item.key: item
            for item in opportunities
            if item.exchange == Exchange.GATE
        }
        gate_positions = sorted(
            (
                position
                for position in positions
                if position.exchange == Exchange.GATE
                and position.environment == config.environment
                and position.status == "open"
                and position.quantity > 0
            ),
            key=lambda position: (
                position.liquidation_buffer is None,
                position.liquidation_buffer or Decimal("Infinity"),
                _utc(position.opened_at),
                position.base_asset,
            ),
        )
        candidates: list[Opportunity] = []
        candidate_keys: set[str] = set()
        for position in gate_positions:
            key = f"{Exchange.GATE.value}:{position.base_asset}"
            opportunity = opportunity_by_key.get(key)
            if (
                opportunity is not None
                and opportunity.key in depth_pair_by_key
                and _tradable_quote(opportunity, now)
            ):
                candidates.append(opportunity)
                candidate_keys.add(opportunity.key)
            if len(candidates) >= GATE_AUTOMATIC_DEPTH_CANDIDATES:
                break
        opening_candidates = [
            item
            for item in _opening_rule_candidates(
                config=config,
                opportunities=opportunities,
                positions=positions,
                blocked_open_keys=blocked_open_keys,
                now=now,
            )
            if item.exchange == Exchange.GATE
            and item.key in depth_pair_by_key
            and item.key not in candidate_keys
        ]
        candidates.extend(
            opening_candidates[
                : GATE_AUTOMATIC_DEPTH_CANDIDATES - len(candidates)
            ]
        )
        if not candidates:
            await adapter.close()
            return opportunities
        refreshed: dict[str, Opportunity] = {}
        try:
            for opportunity in candidates:
                pair = depth_pair_by_key[opportunity.key]
                try:
                    quote = await adapter.executable_quote(
                        pair,
                        market_quote_from_opportunity(opportunity),
                    )
                    if (
                        config.environment
                        == ExchangeEnvironment.SANDBOX.value
                    ):
                        funding = sandbox_funding_by_key[
                            opportunity.key
                        ]
                        try:
                            history = await adapter.funding_history(
                                pair,
                                start=now - timedelta(days=8),
                                end=now,
                            )
                        except (RuntimeError, ValueError):
                            history = []
                        refreshed_item = build_opportunity(
                            pair,
                            quote,
                            funding,
                            history,
                            sandbox_fee,
                            holding_days=sandbox_holding_days,
                            now=now,
                        )
                        if (
                            refreshed_item.quality != Quality.STALE
                            and (
                                refreshed_item.apr_24h is None
                                or refreshed_item.apr_7d is None
                                or refreshed_item.net_return is None
                                or refreshed_item.quality
                                == Quality.WARMING
                            )
                        ):
                            refreshed_item = (
                                apply_current_funding_fallback(
                                    refreshed_item,
                                    fee=sandbox_fee,
                                    holding_days=sandbox_holding_days,
                                )
                            )
                        refreshed[opportunity.key] = refreshed_item
                    else:
                        refreshed[opportunity.key] = (
                            opportunity_with_executable_quote(
                                opportunity,
                                quote,
                            )
                        )
                except (RuntimeError, ValueError):
                    logger.info(
                        "automatic Gate depth unavailable",
                        extra={
                            "exchange": Exchange.GATE.value,
                            "symbol": opportunity.base_asset,
                            "environment": config.environment,
                        },
                    )
        finally:
            await adapter.close()
        return [
            refreshed.get(opportunity.key, opportunity)
            for opportunity in opportunities
        ]




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
