from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import (
    OrderMode,
    PerpMarginMode,
    PerpPositionSide,
    PositionMode,
    PrivateAccountClient,
    PrivateOrderRequest,
    PrivateRequestError,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange, Quality
from basis_hawk.order_books import (
    OrderBookUnavailable,
    RestOrderBookProvider,
)
from basis_hawk.storage import (
    Database,
    ExecutionOrderRow,
    ExecutionRunRow,
    ExecutionTaskLegRow,
    ExecutionTaskRow,
)
from basis_hawk.trading import protective_limit_price

ACTIVE_ORDER_STATUSES = {
    "created",
    "submitted",
    "acknowledged",
    "partially_filled",
    "cancel_pending",
    "unknown",
}
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "rejected", "failed"}


class ResolvedLegMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_quantity: Decimal
    base_multiplier: Decimal
    reference_price: Decimal
    observed_at: datetime


class LiveOrderQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    native_quantity: Decimal
    base_multiplier: Decimal
    limit_price: Decimal | None
    observed_at: datetime


class LiveQuoteProvider(Protocol):
    async def resolve_leg(
        self,
        leg: ExecutionTaskLegRow,
        quantity_mode: str,
    ) -> ResolvedLegMarket: ...

    async def quote_order(
        self,
        leg: ExecutionTaskLegRow,
        *,
        base_quantity: Decimal,
        mode: str,
        environment: str,
        side: str | None = None,
    ) -> LiveOrderQuote: ...


class MultiLegLiveExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    progressed: int
    completed: int
    failed: int


class LatestOpportunityLiveQuoteProvider:
    def __init__(
        self,
        database: Database,
        *,
        maximum_age: timedelta = timedelta(seconds=15),
        order_books: RestOrderBookProvider | None = None,
    ) -> None:
        self.database = database
        self.maximum_age = maximum_age
        self.order_books = order_books or RestOrderBookProvider()

    async def resolve_leg(
        self,
        leg: ExecutionTaskLegRow,
        quantity_mode: str,
    ) -> ResolvedLegMarket:
        opportunity, pair = await self._market(leg)
        reference = (
            opportunity.spot_ask
            if leg.market_type == "spot" and leg.side == "buy"
            else opportunity.spot_bid
            if leg.market_type == "spot"
            else opportunity.perp_ask
            if leg.side == "buy"
            else opportunity.perp_bid
        )
        multiplier = (
            Decimal("1") if leg.market_type == "spot" else pair.perp_contract_size
        )
        increment = (
            pair.spot_quantity_increment
            if leg.market_type == "spot"
            else pair.perp_quantity_increment
        )
        requested_base = (
            leg.target_quantity
            if quantity_mode == "base"
            else leg.target_quantity / reference
        )
        native_quantity = _floor_to_increment(
            requested_base / multiplier,
            increment,
        )
        if native_quantity <= 0:
            raise ValueError("resolved order quantity is below the exchange increment")
        base_quantity = native_quantity * multiplier
        minimum_quantity = (
            pair.spot_min_quantity
            if leg.market_type == "spot"
            else pair.perp_min_quantity
        )
        minimum_notional = (
            pair.spot_min_notional
            if leg.market_type == "spot"
            else pair.perp_min_notional
        )
        if native_quantity < minimum_quantity:
            raise ValueError("resolved order quantity is below the exchange minimum")
        if base_quantity * reference < minimum_notional:
            raise ValueError("resolved order notional is below the exchange minimum")
        return ResolvedLegMarket(
            base_quantity=base_quantity,
            base_multiplier=multiplier,
            reference_price=reference,
            observed_at=opportunity.observed_at,
        )

    async def quote_order(
        self,
        leg: ExecutionTaskLegRow,
        *,
        base_quantity: Decimal,
        mode: str,
        environment: str,
        side: str | None = None,
    ) -> LiveOrderQuote:
        if mode == "maker":
            opportunity = None
            pair = await self._rules(leg)
        else:
            opportunity, pair = await self._market(leg)
        multiplier = (
            Decimal("1") if leg.market_type == "spot" else pair.perp_contract_size
        )
        quantity_increment = (
            pair.spot_quantity_increment
            if leg.market_type == "spot"
            else pair.perp_quantity_increment
        )
        native_quantity = _floor_to_increment(
            base_quantity / multiplier,
            quantity_increment,
        )
        if native_quantity <= 0:
            raise ValueError("remaining quantity is below the exchange increment")
        price_increment = (
            pair.spot_price_increment
            if leg.market_type == "spot"
            else pair.perp_price_increment
        )
        order_side = side or leg.side
        if mode == "market":
            limit_price = None
            observed_at = opportunity.observed_at
        elif mode == "maker":
            level = leg.maker_book_level or 1
            book = await self.order_books.fetch(
                exchange=Exchange(leg.exchange),
                environment=ExchangeEnvironment(environment),
                market="spot" if leg.market_type == "spot" else "perp",
                symbol=leg.symbol,
                level=level,
            )
            limit_price = book.maker_price(side=order_side, level=level)
            observed_at = book.observed_at
        else:
            bid = (
                opportunity.spot_bid
                if leg.market_type == "spot"
                else opportunity.perp_bid
            )
            ask = (
                opportunity.spot_ask
                if leg.market_type == "spot"
                else opportunity.perp_ask
            )
            limit_price = protective_limit_price(
                reference_price=ask if order_side == "buy" else bid,
                maximum_slippage=leg.maximum_slippage,
                side=order_side,
                price_increment=price_increment,
            )
            observed_at = opportunity.observed_at
        if limit_price is not None and limit_price <= 0:
            raise ValueError("resolved order price must be positive")
        return LiveOrderQuote(
            native_quantity=native_quantity,
            base_multiplier=multiplier,
            limit_price=limit_price,
            observed_at=observed_at,
        )

    async def _market(self, leg: ExecutionTaskLegRow):
        opportunities = await self.database.latest_opportunities(
            exchanges={leg.exchange}
        )
        opportunity = next(
            (
                item
                for item in opportunities
                if item.base_asset.upper() == leg.base_asset.upper()
            ),
            None,
        )
        if opportunity is None or opportunity.quality != Quality.HEALTHY:
            raise ValueError("healthy live quote is unavailable")
        observed_at = _utc(opportunity.observed_at)
        now = datetime.now(UTC)
        if (
            observed_at > now + timedelta(seconds=5)
            or now - observed_at > self.maximum_age
        ):
            raise ValueError("live quote is stale")
        pair = await self._rules(leg)
        return opportunity, pair

    async def _rules(self, leg: ExecutionTaskLegRow):
        pairs = await self.database.instrument_pairs(exchanges={leg.exchange})
        pair = next(
            (
                item
                for item in pairs
                if item.base_asset.upper() == leg.base_asset.upper()
            ),
            None,
        )
        if pair is None or not pair.trading_rules_complete:
            raise ValueError("live instrument rules are incomplete")
        expected_symbol = (
            pair.spot_symbol if leg.market_type == "spot" else pair.perp_symbol
        )
        if leg.symbol != expected_symbol:
            raise ValueError("task-leg symbol does not match instrument rules")
        return pair


AccountClientFactory = Callable[
    [Exchange, ExchangeSecrets, ExchangeEnvironment],
    PrivateAccountClient,
]


class MultiLegLiveExecutionService:
    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        *,
        account_client_factory: AccountClientFactory,
        quote_provider: LiveQuoteProvider | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory
        self.quote_provider = quote_provider or LatestOpportunityLiveQuoteProvider(
            database
        )
        self.worker_id = worker_id or f"live-worker-{uuid.uuid4()}"

    async def run_once(self) -> MultiLegLiveExecutionResult:
        claimed = await self.database.claim_live_execution_task(
            worker_id=self.worker_id
        )
        if claimed is None:
            return MultiLegLiveExecutionResult(
                examined=0,
                progressed=0,
                completed=0,
                failed=0,
            )
        task, legs, run, orders = claimed
        try:
            await self._progress(task, legs, run, orders)
        except (ArithmeticError, KeyError, RuntimeError, ValueError):
            current_orders = await self.database.execution_orders_for_run(run.id)
            has_primary_exposure = any(
                item.purpose == "primary" and item.filled_quantity > 0
                for item in current_orders
            )
            has_uncertain_order = any(
                item.status
                in {
                    "submitted",
                    "acknowledged",
                    "partially_filled",
                    "cancel_pending",
                    "unknown",
                }
                for item in current_orders
            )
            if (
                task.status != "compensating"
                and has_primary_exposure
                and not has_uncertain_order
            ):
                await self.database.begin_execution_task_compensation(
                    task_id=task.id,
                    run_id=run.id,
                    failure_code="multi_leg_execution_failed",
                    worker_id=self.worker_id,
                )
                return MultiLegLiveExecutionResult(
                    examined=1,
                    progressed=1,
                    completed=0,
                    failed=0,
                )
            await self.database.fail_execution_task_run(
                task_id=task.id,
                run_id=run.id,
                failure_code="multi_leg_execution_failed",
                worker_id=self.worker_id,
                manual_review=(
                    task.status == "compensating"
                    or has_primary_exposure
                    or has_uncertain_order
                ),
            )
            return MultiLegLiveExecutionResult(
                examined=1,
                progressed=0,
                completed=0,
                failed=1,
            )
        refreshed = await self.database.execution_task(task.id)
        completed = int(refreshed is not None and refreshed[0].status == "completed")
        return MultiLegLiveExecutionResult(
            examined=1,
            progressed=1,
            completed=completed,
            failed=0,
        )

    async def _progress(
        self,
        task: ExecutionTaskRow,
        legs: list[ExecutionTaskLegRow],
        run: ExecutionRunRow,
        orders: list[ExecutionOrderRow],
    ) -> None:
        if task.status != "compensating":
            await self._resolve_legs(task, legs)
        active = next(
            (item for item in orders if item.status in ACTIVE_ORDER_STATUSES),
            None,
        )
        if active is not None:
            leg = next(item for item in legs if item.id == active.task_leg_id)
            await self._progress_order(task, leg, active)
            return
        if task.status == "compensating":
            await self._progress_compensation(task, legs, run, orders)
            return
        filled_by_leg = {
            leg.id: sum(
                (
                    order.filled_quantity * order.base_multiplier
                    for order in orders
                    if order.task_leg_id == leg.id and order.purpose == "primary"
                ),
                Decimal("0"),
            )
            for leg in legs
        }
        anchor = next(item for item in legs if item.role == "anchor")
        anchor_target = _target(anchor)
        anchor_filled = filled_by_leg[anchor.id]
        for leg in (item for item in legs if item.role == "hedge"):
            hedge_target = _target(leg)
            desired = min(
                hedge_target,
                anchor_filled * hedge_target / anchor_target,
            )
            if (
                task.hedge_trigger == "cumulative_percent"
                and anchor_filled < anchor_target
                and anchor_filled / anchor_target < Decimal(task.hedge_threshold or 1)
            ):
                desired = Decimal("0")
            deficit = desired - filled_by_leg[leg.id]
            if deficit > 0:
                await self.database.set_execution_task_phase(
                    task_id=task.id,
                    status="hedging",
                )
                await self._create_next_order(task, run, leg, deficit, orders)
                return
        anchor_remaining = anchor_target - anchor_filled
        if anchor_remaining > 0:
            await self.database.set_execution_task_phase(
                task_id=task.id,
                status="running",
            )
            await self._create_next_order(
                task,
                run,
                anchor,
                anchor_remaining,
                orders,
            )
            return
        for leg in (item for item in legs if item.role == "hedge"):
            deficit = _target(leg) - filled_by_leg[leg.id]
            if deficit > 0:
                await self.database.set_execution_task_phase(
                    task_id=task.id,
                    status="hedging",
                )
                await self._create_next_order(task, run, leg, deficit, orders)
                return
        await self.database.complete_live_execution_task(
            task_id=task.id,
            run_id=run.id,
            worker_id=self.worker_id,
        )

    async def _resolve_legs(
        self,
        task: ExecutionTaskRow,
        legs: list[ExecutionTaskLegRow],
    ) -> None:
        resolved: dict[str, ResolvedLegMarket] = {}
        for leg in legs:
            market = await self.quote_provider.resolve_leg(
                leg,
                task.quantity_mode,
            )
            if leg.resolved_base_quantity is not None:
                market = market.model_copy(
                    update={"base_quantity": leg.resolved_base_quantity}
                )
            resolved[leg.id] = market
        anchor = next(item for item in legs if item.role == "anchor")
        anchor_quantity = resolved[anchor.id].base_quantity
        for leg in legs:
            sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
            ratio = sign * resolved[leg.id].base_quantity / anchor_quantity
            if leg.resolved_base_quantity is None:
                updated = await self.database.resolve_execution_task_leg_quantity(
                    task_leg_id=leg.id,
                    base_quantity=resolved[leg.id].base_quantity,
                    signed_base_ratio=ratio,
                )
                leg.resolved_base_quantity = updated.resolved_base_quantity
                leg.signed_base_ratio = updated.signed_base_ratio
            elif (
                leg.resolved_base_quantity != resolved[leg.id].base_quantity
                or leg.signed_base_ratio != ratio
            ):
                raise ValueError("resolved live task quantity changed")
        base_exposure = abs(
            sum(
                (
                    (Decimal("1") if leg.side == "buy" else Decimal("-1"))
                    * resolved[leg.id].base_quantity
                    for leg in legs
                ),
                Decimal("0"),
            )
        )
        notional_exposure = abs(
            sum(
                (
                    (Decimal("1") if leg.side == "buy" else Decimal("-1"))
                    * resolved[leg.id].base_quantity
                    * resolved[leg.id].reference_price
                    for leg in legs
                ),
                Decimal("0"),
            )
        )
        if base_exposure > task.maximum_base_exposure:
            raise ValueError("resolved live base exposure exceeds the task limit")
        if notional_exposure > task.maximum_notional_exposure_usdt:
            raise ValueError("resolved live notional exposure exceeds the task limit")

    async def _create_next_order(
        self,
        task: ExecutionTaskRow,
        run: ExecutionRunRow,
        leg: ExecutionTaskLegRow,
        deficit: Decimal,
        orders: list[ExecutionOrderRow],
    ) -> None:
        leg_orders = [item for item in orders if item.task_leg_id == leg.id]
        latest = leg_orders[-1] if leg_orders else None
        mode = leg.order_mode
        parent_id: str | None = None
        if (
            latest is not None
            and latest.order_mode == "maker"
            and latest.status in TERMINAL_ORDER_STATUSES
            and latest.filled_quantity < latest.quantity
        ):
            parent_id = latest.id
            if latest.chase_number >= (leg.maker_maximum_chases or 0):
                if leg.maker_fallback_mode is None:
                    raise ValueError("maker chase limit reached without fallback")
                mode = leg.maker_fallback_mode
            else:
                mode = "maker"
        elif latest is not None and latest.status in {
            "canceled",
            "rejected",
            "failed",
        }:
            attempts = max(item.attempt_number for item in leg_orders)
            if attempts > task.maximum_retries:
                raise ValueError("execution task retry limit reached")
        chunk = deficit
        if leg.per_order_quantity > 0:
            leg_target = _target(leg)
            chunk_base = (
                leg.per_order_quantity
                if task.quantity_mode == "base"
                else leg_target * leg.per_order_quantity / leg.target_quantity
            )
            chunk = min(chunk, chunk_base)
        quote = await self.quote_provider.quote_order(
            leg,
            base_quantity=chunk,
            mode=mode,
            environment=task.environment,
        )
        token = uuid.uuid4().hex
        client_order_id = _client_order_id(Exchange(leg.exchange), token)
        await self.database.create_execution_order_attempt(
            run_id=run.id,
            task_leg_id=leg.id,
            client_order_id=client_order_id,
            order_mode=mode,
            side=leg.side,
            reduce_only=leg.reduce_only,
            purpose="primary",
            quantity=quote.native_quantity,
            base_multiplier=quote.base_multiplier,
            limit_price=quote.limit_price,
            parent_order_id=parent_id,
        )

    async def _progress_compensation(
        self,
        task: ExecutionTaskRow,
        legs: list[ExecutionTaskLegRow],
        run: ExecutionRunRow,
        orders: list[ExecutionOrderRow],
    ) -> None:
        for leg in legs:
            primary_base = sum(
                (
                    order.filled_quantity * order.base_multiplier
                    for order in orders
                    if order.task_leg_id == leg.id and order.purpose == "primary"
                ),
                Decimal("0"),
            )
            compensated_base = sum(
                (
                    order.filled_quantity * order.base_multiplier
                    for order in orders
                    if order.task_leg_id == leg.id and order.purpose == "compensation"
                ),
                Decimal("0"),
            )
            remaining = primary_base - compensated_base
            if remaining < 0:
                raise ValueError("compensation exceeded the primary fill")
            if remaining == 0:
                continue
            attempts = [
                item
                for item in orders
                if item.task_leg_id == leg.id and item.purpose == "compensation"
            ]
            if len(attempts) > task.maximum_retries:
                raise ValueError("compensation retry limit reached")
            side = "sell" if leg.side == "buy" else "buy"
            quote = await self.quote_provider.quote_order(
                leg,
                base_quantity=remaining,
                mode="protected_ioc",
                environment=task.environment,
                side=side,
            )
            token = uuid.uuid4().hex
            await self.database.create_execution_order_attempt(
                run_id=run.id,
                task_leg_id=leg.id,
                client_order_id=_client_order_id(
                    Exchange(leg.exchange),
                    token,
                ),
                order_mode="protected_ioc",
                side=side,
                reduce_only=leg.market_type == "perpetual",
                purpose="compensation",
                quantity=quote.native_quantity,
                base_multiplier=quote.base_multiplier,
                limit_price=quote.limit_price,
            )
            return
        await self.database.fail_execution_task_run(
            task_id=task.id,
            run_id=run.id,
            failure_code=task.failure_code or "multi_leg_execution_compensated",
            worker_id=self.worker_id,
            manual_review=False,
        )

    async def _progress_order(
        self,
        task: ExecutionTaskRow,
        leg: ExecutionTaskLegRow,
        order: ExecutionOrderRow,
    ) -> None:
        if leg.account_id is None:
            raise ValueError("live execution task leg has no account")
        summary = await self.credentials.summary(leg.account_id)
        secrets = await self.credentials.load_by_id(leg.account_id)
        if summary is None or secrets is None:
            raise ValueError("live execution account is not configured")
        if (
            summary.exchange.value != leg.exchange
            or summary.environment.value != task.environment
        ):
            raise ValueError("live execution account scope changed")
        client = self.account_client_factory(
            summary.exchange,
            secrets,
            summary.environment,
        )
        try:
            lookup = await client.order_by_client_id(
                market="spot" if leg.market_type == "spot" else "perp",
                symbol=leg.symbol,
                client_order_id=order.client_order_id,
            )
            if not lookup.complete:
                await self.database.mark_execution_order_unknown(order_id=order.id)
                return
            if lookup.order is None:
                if order.status == "cancel_pending":
                    fill_batch = await client.fills_for_order(
                        market=("spot" if leg.market_type == "spot" else "perp"),
                        symbol=leg.symbol,
                        exchange_order_id=order.exchange_order_id,
                        client_order_id=order.client_order_id,
                        since=_utc(order.created_at),
                    )
                    if not fill_batch.complete:
                        await self.database.mark_execution_order_unknown(
                            order_id=order.id
                        )
                        return
                    filled_quantity = sum(
                        (item.quantity for item in fill_batch.fills),
                        Decimal("0"),
                    )
                    average_price = (
                        sum(
                            (item.quantity * item.price for item in fill_batch.fills),
                            Decimal("0"),
                        )
                        / filled_quantity
                        if filled_quantity > 0
                        else order.average_price
                    )
                    await self.database.apply_execution_order_observation(
                        order_id=order.id,
                        exchange_order_id=order.exchange_order_id,
                        status="canceled",
                        filled_quantity=filled_quantity,
                        average_price=average_price,
                        fills=[
                            {
                                "exchange_trade_id": item.exchange_trade_id,
                                "quantity": item.quantity,
                                "price": item.price,
                                "fee_amount": item.fee_amount,
                                "fee_asset": item.fee_asset,
                                "liquidity": item.liquidity,
                                "occurred_at": item.occurred_at,
                            }
                            for item in fill_batch.fills
                        ],
                    )
                    return
                if order.status not in {"created", "unknown"}:
                    await self.database.mark_execution_order_unknown(order_id=order.id)
                    return
                await self._submit_order(client, task, leg, order)
                return
            remote = lookup.order
            fill_batch = await client.fills_for_order(
                market="spot" if leg.market_type == "spot" else "perp",
                symbol=leg.symbol,
                exchange_order_id=remote.exchange_order_id,
                client_order_id=order.client_order_id,
                since=_utc(order.created_at),
            )
            if not fill_batch.complete:
                await self.database.mark_execution_order_unknown(order_id=order.id)
                return
            filled_quantity = sum(
                (item.quantity for item in fill_batch.fills),
                Decimal("0"),
            )
            average_price = (
                sum(
                    (item.quantity * item.price for item in fill_batch.fills),
                    Decimal("0"),
                )
                / filled_quantity
                if filled_quantity > 0
                else remote.price
                if remote.price > 0
                else None
            )
            status = _remote_status(
                remote.status,
                filled_quantity=filled_quantity,
                requested_quantity=order.quantity,
            )
            if order.status == "cancel_pending" and status in {
                "acknowledged",
                "partially_filled",
                "unknown",
            }:
                status = "cancel_pending"
            observed = await self.database.apply_execution_order_observation(
                order_id=order.id,
                exchange_order_id=remote.exchange_order_id,
                status=status,
                filled_quantity=filled_quantity,
                average_price=average_price,
                fills=[
                    {
                        "exchange_trade_id": item.exchange_trade_id,
                        "quantity": item.quantity,
                        "price": item.price,
                        "fee_amount": item.fee_amount,
                        "fee_asset": item.fee_asset,
                        "liquidity": item.liquidity,
                        "occurred_at": item.occurred_at,
                    }
                    for item in fill_batch.fills
                ],
            )
            if (
                observed.order_mode == "maker"
                and observed.status in {"acknowledged", "partially_filled"}
                and order.status != "cancel_pending"
            ):
                try:
                    latest_quote = await self.quote_provider.quote_order(
                        leg,
                        base_quantity=(
                            observed.quantity - observed.filled_quantity
                        )
                        * observed.base_multiplier,
                        mode="maker",
                        environment=task.environment,
                    )
                except OrderBookUnavailable:
                    return
                if not _maker_is_outside_book_level(
                    side=observed.side,
                    order_price=observed.limit_price,
                    book_level_price=latest_quote.limit_price,
                ):
                    return
                await client.cancel_order(
                    market="spot" if leg.market_type == "spot" else "perp",
                    symbol=leg.symbol,
                    exchange_order_id=remote.exchange_order_id,
                    client_order_id=order.client_order_id,
                )
                await self.database.mark_execution_order_cancel_pending(
                    order_id=order.id
                )
        finally:
            await client.close()

    async def _submit_order(
        self,
        client: PrivateAccountClient,
        task: ExecutionTaskRow,
        leg: ExecutionTaskLegRow,
        order: ExecutionOrderRow,
    ) -> None:
        snapshot = await client.snapshot()
        if snapshot.trade_permission is False:
            raise ValueError("live execution account cannot trade")
        if leg.market_type == "spot" and order.side == "sell":
            available = await client.spot_asset_available(leg.base_asset)
            required = order.quantity * order.base_multiplier
            if available < required:
                raise ValueError("spot inventory is insufficient for sell order")
        if leg.market_type == "perpetual":
            if snapshot.position_mode == PositionMode.UNKNOWN:
                raise ValueError("live execution position mode is unknown")
            requested_margin = PerpMarginMode(leg.margin_mode or "isolated")
            exchange = Exchange(leg.exchange)
            symbol_scoped_binance_cross = (
                exchange == Exchange.BINANCE
                and requested_margin == PerpMarginMode.CROSS
            )
            if (
                snapshot.perp_margin_mode != requested_margin
                and not symbol_scoped_binance_cross
            ):
                raise ValueError("live execution margin mode changed after preflight")
            if (
                requested_margin == PerpMarginMode.CROSS
                and exchange not in {Exchange.BINANCE, Exchange.GATE}
            ):
                raise ValueError(
                    f"{exchange.value} cross-margin leverage confirmation "
                    "is not implemented"
                )
            if symbol_scoped_binance_cross:
                configuration = await client.configure_perp(
                    symbol=leg.symbol,
                    leverage=leg.leverage,
                    position_mode=snapshot.position_mode,
                    margin_mode=requested_margin,
                )
            else:
                configuration = await client.configure_perp(
                    symbol=leg.symbol,
                    leverage=leg.leverage,
                    position_mode=snapshot.position_mode,
                )
            if (
                configuration.isolated
                != (requested_margin == PerpMarginMode.ISOLATED)
                or configuration.leverage != leg.leverage
                or configuration.position_mode != snapshot.position_mode
            ):
                raise ValueError(
                    "live execution perpetual configuration was not confirmed"
                )
        else:
            requested_margin = PerpMarginMode.ISOLATED
        try:
            submission = await client.place_order(
                PrivateOrderRequest(
                    market=("spot" if leg.market_type == "spot" else "perp"),
                    symbol=leg.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    mode=OrderMode(order.order_mode),
                    limit_price=order.limit_price,
                    client_order_id=order.client_order_id,
                    reduce_only=order.reduce_only,
                    position_mode=(
                        snapshot.position_mode
                        if leg.market_type == "perpetual"
                        else PositionMode.UNKNOWN
                    ),
                    position_side=(
                        _position_side(
                            side=order.side,
                            reduce_only=order.reduce_only,
                        )
                        if leg.market_type == "perpetual"
                        else None
                    ),
                    margin_mode=requested_margin,
                )
            )
        except PrivateRequestError:
            await self.database.mark_execution_order_unknown(order_id=order.id)
            return
        await self.database.mark_execution_order_submitted(
            order_id=order.id,
            exchange_order_id=submission.exchange_order_id,
        )


def _target(leg: ExecutionTaskLegRow) -> Decimal:
    if leg.resolved_base_quantity is None:
        raise ValueError("task-leg base quantity is unresolved")
    return leg.resolved_base_quantity


def _maker_is_outside_book_level(
    *,
    side: str,
    order_price: Decimal | None,
    book_level_price: Decimal | None,
) -> bool:
    if order_price is None or book_level_price is None:
        raise ValueError("maker price comparison requires two limit prices")
    return (
        order_price < book_level_price
        if side == "buy"
        else order_price > book_level_price
    )


def _position_side(
    *,
    side: str,
    reduce_only: bool,
) -> PerpPositionSide:
    return (
        PerpPositionSide.LONG
        if (side == "buy") != reduce_only
        else PerpPositionSide.SHORT
    )


def _client_order_id(exchange: Exchange, token: str) -> str:
    if exchange == Exchange.OKX:
        return f"bh{token[:28]}"
    if exchange == Exchange.GATE:
        return f"t-bh-{token[:20]}"
    return f"bh-{token[:28]}"


def _remote_status(
    value: str,
    *,
    filled_quantity: Decimal,
    requested_quantity: Decimal,
) -> str:
    if filled_quantity >= requested_quantity:
        return "filled"
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"filled", "closed", "done"}:
        return "filled"
    if normalized in {
        "canceled",
        "cancelled",
        "expired",
        "deactivated",
    }:
        return "canceled"
    if normalized in {"rejected", "reject"}:
        return "rejected"
    if normalized in {"failed", "error"}:
        return "failed"
    if filled_quantity > 0 or normalized in {
        "partially_filled",
        "partial_fill",
        "partial",
    }:
        return "partially_filled"
    if normalized in {
        "new",
        "open",
        "live",
        "active",
        "created",
        "submitted",
        "acknowledged",
    }:
        return "acknowledged"
    return "unknown"


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("quantity increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
