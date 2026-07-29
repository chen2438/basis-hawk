from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from basis_hawk.models import Exchange, Quality
from basis_hawk.storage import Database, ExecutionTaskLegRow


class PaperExecutionQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    native_quantity: Decimal
    base_multiplier: Decimal
    fee_usdt: Decimal
    observed_at: datetime


PaperQuoteProvider = Callable[
    [ExecutionTaskLegRow, str],
    Awaitable[PaperExecutionQuote],
]


class MultiLegPaperExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    completed: int
    failed: int


class LatestOpportunityPaperQuoteProvider:
    def __init__(
        self,
        database: Database,
        *,
        maximum_age: timedelta = timedelta(seconds=15),
    ) -> None:
        self.database = database
        self.maximum_age = maximum_age

    async def __call__(
        self,
        leg: ExecutionTaskLegRow,
        quantity_mode: str,
    ) -> PaperExecutionQuote:
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
            raise ValueError("healthy paper quote is unavailable")
        observed_at = _utc(opportunity.observed_at)
        now = datetime.now(UTC)
        if observed_at > now + timedelta(seconds=5) or now - observed_at > self.maximum_age:
            raise ValueError("paper quote is stale")
        is_maker = leg.order_mode == "maker"
        if leg.market_type == "spot":
            if leg.symbol != opportunity.spot_symbol:
                raise ValueError("paper spot symbol does not match market data")
            price = (
                opportunity.spot_bid
                if (is_maker and leg.side == "buy")
                else opportunity.spot_ask
                if (is_maker and leg.side == "sell")
                else opportunity.spot_ask
                if leg.side == "buy"
                else opportunity.spot_bid
            )
            base_multiplier = Decimal("1")
        else:
            if leg.symbol != opportunity.perp_symbol:
                raise ValueError("paper perpetual symbol does not match market data")
            price = (
                opportunity.perp_bid
                if (is_maker and leg.side == "buy")
                else opportunity.perp_ask
                if (is_maker and leg.side == "sell")
                else opportunity.perp_ask
                if leg.side == "buy"
                else opportunity.perp_bid
            )
            pairs = await self.database.instrument_pairs(
                exchanges={leg.exchange}
            )
            pair = next(
                (
                    item
                    for item in pairs
                    if item.base_asset.upper() == leg.base_asset.upper()
                    and item.perp_symbol == leg.symbol
                ),
                None,
            )
            if pair is None or pair.perp_contract_size <= 0:
                raise ValueError("paper perpetual contract multiplier is unavailable")
            base_multiplier = pair.perp_contract_size
        if price <= 0:
            raise ValueError("paper quote price must be positive")
        base_quantity = (
            leg.target_quantity
            if quantity_mode == "base"
            else leg.target_quantity / price
        )
        native_quantity = base_quantity / base_multiplier
        settings = await self.database.load_settings()
        fees = settings.fees[Exchange(leg.exchange)]
        fee_rate = (
            fees.spot_taker
            if leg.market_type == "spot"
            else fees.perp_taker
        )
        return PaperExecutionQuote(
            price=price,
            native_quantity=native_quantity,
            base_multiplier=base_multiplier,
            fee_usdt=base_quantity * price * fee_rate,
            observed_at=observed_at,
        )


class MultiLegPaperExecutionService:
    def __init__(
        self,
        database: Database,
        *,
        quote_provider: PaperQuoteProvider | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.quote_provider = quote_provider or LatestOpportunityPaperQuoteProvider(
            database
        )
        self.worker_id = worker_id or f"paper-worker-{uuid.uuid4()}"

    async def run_once(self) -> MultiLegPaperExecutionResult:
        claimed = await self.database.claim_paper_execution_task(
            worker_id=self.worker_id
        )
        if claimed is None:
            return MultiLegPaperExecutionResult(
                examined=0,
                completed=0,
                failed=0,
            )
        task, legs, run = claimed
        try:
            quotes = [
                await self.quote_provider(leg, task.quantity_mode)
                for leg in legs
            ]
            await self.database.complete_paper_execution_task(
                task_id=task.id,
                run_id=run.id,
                fills=[
                    {
                        "task_leg_id": leg.id,
                        "native_quantity": quote.native_quantity,
                        "base_multiplier": quote.base_multiplier,
                        "price": quote.price,
                        "fee_usdt": quote.fee_usdt,
                    }
                    for leg, quote in zip(legs, quotes, strict=True)
                ],
                worker_id=self.worker_id,
            )
        except (ArithmeticError, KeyError, ValueError):
            await self.database.fail_execution_task_run(
                task_id=task.id,
                run_id=run.id,
                failure_code="paper_quote_unavailable",
                worker_id=self.worker_id,
            )
            return MultiLegPaperExecutionResult(
                examined=1,
                completed=0,
                failed=1,
            )
        return MultiLegPaperExecutionResult(
            examined=1,
            completed=1,
            failed=0,
        )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
