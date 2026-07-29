from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from basis_hawk.multi_leg import DecimalPayload
from basis_hawk.storage import (
    ArbitrageStrategyRow,
    Database,
    ExecutionTaskLegRow,
    StrategyLegRow,
)


class StrategyLegView(DecimalPayload):
    id: str
    ordinal: int
    opening_task_leg_id: str
    account_id: str | None
    exchange: str
    role: str
    market_type: str
    side: str
    symbol: str
    initial_base_quantity: Decimal
    remaining_base_quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    fees_usdt: Decimal
    realized_pnl_usdt: Decimal


class StrategyView(DecimalPayload):
    id: str
    name: str
    environment: str
    base_asset: str
    opening_task_id: str
    closing_task_id: str | None
    status: str
    realized_pnl_usdt: Decimal
    funding_income_usdt: Decimal
    fees_usdt: Decimal
    net_pnl_usdt: Decimal
    opened_at: datetime
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    legs: list[StrategyLegView]


class PortfolioService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, strategy_id: str) -> StrategyView | None:
        value = await self.database.arbitrage_strategy(strategy_id)
        return _view(*value) if value is not None else None

    async def list(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[StrategyView]:
        return [
            _view(strategy, legs, opening_legs)
            for strategy, legs, opening_legs in (
                await self.database.arbitrage_strategy_rows(
                    statuses=statuses,
                    limit=limit,
                )
            )
        ]


def _view(
    strategy: ArbitrageStrategyRow,
    legs: list[StrategyLegRow],
    opening_legs: dict[str, ExecutionTaskLegRow],
) -> StrategyView:
    return StrategyView(
        id=strategy.id,
        name=strategy.name,
        environment=strategy.environment,
        base_asset=strategy.base_asset,
        opening_task_id=strategy.opening_task_id,
        closing_task_id=strategy.closing_task_id,
        status=strategy.status,
        realized_pnl_usdt=strategy.realized_pnl_usdt,
        funding_income_usdt=strategy.funding_income_usdt,
        fees_usdt=strategy.fees_usdt,
        net_pnl_usdt=(
            strategy.realized_pnl_usdt
            + strategy.funding_income_usdt
            - strategy.fees_usdt
        ),
        opened_at=strategy.opened_at,
        closed_at=strategy.closed_at,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
        legs=[
            _leg_view(leg, opening_legs[leg.opening_task_leg_id])
            for leg in legs
            if leg.opening_task_leg_id in opening_legs
        ],
    )


def _leg_view(
    leg: StrategyLegRow,
    opening_leg: ExecutionTaskLegRow,
) -> StrategyLegView:
    return StrategyLegView(
        id=leg.id,
        ordinal=leg.ordinal,
        opening_task_leg_id=leg.opening_task_leg_id,
        account_id=leg.account_id,
        exchange=opening_leg.exchange,
        role=opening_leg.role,
        market_type=leg.market_type,
        side=leg.side,
        symbol=leg.symbol,
        initial_base_quantity=leg.initial_base_quantity,
        remaining_base_quantity=leg.remaining_base_quantity,
        entry_price=leg.entry_price,
        exit_price=leg.exit_price,
        fees_usdt=leg.fees_usdt,
        realized_pnl_usdt=leg.realized_pnl_usdt,
    )
