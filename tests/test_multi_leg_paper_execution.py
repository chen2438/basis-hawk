from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from basis_hawk.credentials import CredentialService
from basis_hawk.crypto import SecretCipher
from basis_hawk.execution_tasks import ExecutionTaskService
from basis_hawk.multi_leg import ExecutionTaskSpec
from basis_hawk.multi_leg_execution import (
    MultiLegPaperExecutionService,
    PaperExecutionQuote,
)
from basis_hawk.storage import (
    ArbitrageStrategyRow,
    Database,
    ExecutionFillRow,
    ExecutionOrderRow,
    StrategyLegRow,
)


def _spec() -> ExecutionTaskSpec:
    return ExecutionTaskSpec.model_validate(
        {
            "name": "three-leg paper carry",
            "display_symbol": "BTC/USDT",
            "environment": "paper",
            "base_asset": "BTC",
            "quantity_mode": "base",
            "maximum_base_exposure": "0.001",
            "maximum_notional_exposure_usdt": "1000",
            "legs": [
                {
                    "exchange": "binance",
                    "role": "anchor",
                    "market_type": "spot",
                    "side": "buy",
                    "base_asset": "BTC",
                    "symbol": "BTCUSDT",
                    "target_quantity": "0.01",
                    "order_mode": "maker",
                    "maker_policy": {
                        "book_level": 3,
                        "maximum_chases": 50,
                        "fallback_mode": "protected_ioc",
                    },
                },
                {
                    "exchange": "okx",
                    "role": "hedge",
                    "market_type": "perpetual",
                    "side": "sell",
                    "base_asset": "BTC",
                    "symbol": "BTC-USDT-SWAP",
                    "target_quantity": "0.006",
                    "order_mode": "protected_ioc",
                    "margin_mode": "cross",
                    "leverage": 3,
                },
                {
                    "exchange": "bybit",
                    "role": "hedge",
                    "market_type": "perpetual",
                    "side": "sell",
                    "base_asset": "BTC",
                    "symbol": "BTCUSDT",
                    "target_quantity": "0.004",
                    "order_mode": "market",
                    "margin_mode": "isolated",
                    "leverage": 2,
                },
            ],
        }
    )


async def test_paper_executor_persists_three_leg_strategy_atomically() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    tasks = ExecutionTaskService(
        database,
        credentials,
        lambda exchange, secrets, environment: None,
    )
    task, created = await tasks.create(
        spec=_spec(),
        idempotency_key=uuid4(),
        actor="admin",
    )
    assert created is True
    ready = await tasks.preflight(task_id=task.id, actor="admin")
    queued = await tasks.start(
        task_id=task.id,
        expected_version=ready.version,
        actor="admin",
    )
    assert queued.status == "queued"

    async def quote(leg, quantity_mode):
        assert quantity_mode == "base"
        price = {
            "binance": Decimal("50000"),
            "okx": Decimal("50010"),
            "bybit": Decimal("50020"),
        }[leg.exchange]
        multiplier = (
            Decimal("0.001")
            if leg.exchange == "okx"
            else Decimal("1")
        )
        return PaperExecutionQuote(
            price=price,
            native_quantity=leg.target_quantity / multiplier,
            base_multiplier=multiplier,
            fee_usdt=leg.target_quantity * price * Decimal("0.001"),
            observed_at=datetime.now(UTC),
        )

    executor = MultiLegPaperExecutionService(
        database,
        quote_provider=quote,
        worker_id="paper-test-worker",
    )
    result = await executor.run_once()
    assert result.completed == 1
    completed = await tasks.get(task.id)
    assert completed is not None
    assert completed.status == "completed"

    async with database.sessions() as session:
        strategy = await session.scalar(
            select(ArbitrageStrategyRow).where(
                ArbitrageStrategyRow.opening_task_id == task.id
            )
        )
        assert strategy is not None
        assert (
            abs(strategy.fees_usdt - Decimal("1.00014"))
            < Decimal("0.000000000001")
        )
        strategy_legs = list(
            await session.scalars(
                select(StrategyLegRow)
                .where(StrategyLegRow.strategy_id == strategy.id)
                .order_by(StrategyLegRow.ordinal)
            )
        )
        assert [item.remaining_base_quantity for item in strategy_legs] == [
            Decimal("0.010000000000000000"),
            Decimal("0.006000000000000000"),
            Decimal("0.004000000000000000"),
        ]
        assert await session.scalar(
            select(func.count(ExecutionOrderRow.id))
        ) == 3
        assert await session.scalar(
            select(func.count(ExecutionFillRow.id))
        ) == 3
    assert (await executor.run_once()).examined == 0
    await database.close()
