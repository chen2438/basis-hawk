from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from basis_hawk.multi_leg import (
    ExecutionEnvironment,
    ExecutionTaskLegSpec,
    ExecutionTaskSpec,
    HedgeTriggerMode,
    LegRole,
    LegSide,
    MakerPolicy,
    MarginMode,
    MarketType,
    OrderMode,
    QuantityMode,
)
from basis_hawk.storage import Database


def _spot_anchor(*, account_id: str | None = None) -> ExecutionTaskLegSpec:
    return ExecutionTaskLegSpec(
        account_id=account_id,
        role=LegRole.ANCHOR,
        market_type=MarketType.SPOT,
        side=LegSide.BUY,
        base_asset="BTC",
        symbol="BTCUSDT",
        target_quantity=Decimal("1"),
        order_mode=OrderMode.MAKER,
        maker_policy=MakerPolicy(),
    )


def _perpetual_hedge(
    *,
    account_id: str | None = None,
    quantity: str = "1",
) -> ExecutionTaskLegSpec:
    return ExecutionTaskLegSpec(
        account_id=account_id,
        role=LegRole.HEDGE,
        market_type=MarketType.PERPETUAL,
        side=LegSide.SELL,
        base_asset="BTC",
        symbol="BTC-USDT-SWAP",
        target_quantity=Decimal(quantity),
        order_mode=OrderMode.PROTECTED_IOC,
        margin_mode=MarginMode.ISOLATED,
    )


def test_accepts_one_anchor_and_multiple_weighted_hedges() -> None:
    task = ExecutionTaskSpec(
        name="BTC 三腿费率套利",
        display_symbol="BTC/USDT",
        environment=ExecutionEnvironment.PAPER,
        base_asset="BTC",
        quantity_mode=QuantityMode.BASE,
        legs=[
            _spot_anchor(),
            _perpetual_hedge(quantity="0.4"),
            _perpetual_hedge(quantity="0.6"),
        ],
        maximum_base_exposure=Decimal("0.01"),
        maximum_notional_exposure_usdt=Decimal("100"),
    )

    assert task.anchor.market_type == MarketType.SPOT
    assert len(task.legs) == 3
    assert task.legs[0].maker_policy == MakerPolicy(
        book_level=3,
        maximum_chases=50,
        fallback_mode=OrderMode.PROTECTED_IOC,
    )


def test_rejects_multiple_anchor_legs() -> None:
    with pytest.raises(ValidationError, match="exactly one anchor"):
        ExecutionTaskSpec(
            name="invalid",
            display_symbol="BTC/USDT",
            environment=ExecutionEnvironment.PAPER,
            base_asset="BTC",
            quantity_mode=QuantityMode.BASE,
            legs=[_spot_anchor(), _spot_anchor()],
            maximum_base_exposure=Decimal("1"),
            maximum_notional_exposure_usdt=Decimal("100"),
        )


def test_rejects_unbounded_or_imbalanced_base_exposure() -> None:
    with pytest.raises(ValidationError, match="planned base delta"):
        ExecutionTaskSpec(
            name="imbalanced",
            display_symbol="BTC/USDT",
            environment=ExecutionEnvironment.PAPER,
            base_asset="BTC",
            quantity_mode=QuantityMode.BASE,
            legs=[_spot_anchor(), _perpetual_hedge(quantity="0.5")],
            maximum_base_exposure=Decimal("0.1"),
            maximum_notional_exposure_usdt=Decimal("100"),
        )


def test_live_tasks_require_an_account_for_every_leg() -> None:
    with pytest.raises(ValidationError, match="require an account"):
        ExecutionTaskSpec(
            name="live",
            display_symbol="BTC/USDT",
            environment=ExecutionEnvironment.LIVE,
            base_asset="BTC",
            quantity_mode=QuantityMode.BASE,
            legs=[_spot_anchor(account_id="spot"), _perpetual_hedge()],
            maximum_base_exposure=Decimal("0.01"),
            maximum_notional_exposure_usdt=Decimal("100"),
        )


def test_cumulative_hedging_requires_a_percentage() -> None:
    with pytest.raises(ValidationError, match="requires a threshold"):
        ExecutionTaskSpec(
            name="threshold",
            display_symbol="BTC/USDT",
            environment=ExecutionEnvironment.PAPER,
            base_asset="BTC",
            quantity_mode=QuantityMode.BASE,
            legs=[_spot_anchor(), _perpetual_hedge()],
            hedge_trigger=HedgeTriggerMode.CUMULATIVE_PERCENT,
            maximum_base_exposure=Decimal("0.01"),
            maximum_notional_exposure_usdt=Decimal("100"),
        )


async def test_v2_storage_tables_are_part_of_sqlite_test_schema() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    async with database.engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
    await database.close()

    assert {
        "execution_tasks",
        "execution_task_legs",
        "execution_runs",
        "execution_orders",
        "execution_fills",
        "arbitrage_strategies",
        "strategy_legs",
        "strategy_pnl_events",
        "adl_snapshots",
    }.issubset(table_names)

