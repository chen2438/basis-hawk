import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from basis_hawk.models import Exchange, Opportunity, Quality, ScannerSettings
from basis_hawk.storage import Database
from basis_hawk.trading import (
    IdempotencyConflict,
    PaperExecutionService,
    StateConflict,
    TradeIntentStatus,
    TradeLedger,
    TradeValidationError,
)


def _opportunity(*, observed_at: datetime | None = None) -> Opportunity:
    return Opportunity(
        exchange=Exchange.BINANCE,
        base_asset="ORDER",
        spot_symbol="ORDERUSDT",
        perp_symbol="ORDERUSDT",
        observed_at=observed_at or datetime.now(UTC),
        spot_bid=Decimal("0.049"),
        spot_ask=Decimal("0.05"),
        perp_bid=Decimal("0.051"),
        perp_ask=Decimal("0.052"),
        executable_basis=Decimal("0.02"),
        top_book_notional=Decimal("500"),
        close_top_book_notional=Decimal("500"),
        current_funding_rate=Decimal("0.0001"),
        funding_interval_hours=Decimal("8"),
        next_funding_at=None,
        current_apr=Decimal("0.1095"),
        apr_24h=Decimal("0.1095"),
        apr_7d=Decimal("0.1095"),
        net_return=Decimal("0.006"),
        spot_quote_volume_24h=Decimal("2000000"),
        perp_quote_volume_24h=Decimal("3000000"),
        spot_taker_fee=Decimal("0.001"),
        perp_taker_fee=Decimal("0.0005"),
        quality=Quality.HEALTHY,
    )


async def test_paper_intent_is_persisted_before_execution_and_idempotent() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    key = uuid.uuid4()
    opportunity = _opportunity()

    first, created = await ledger.plan_paper_open(
        opportunity=opportunity,
        notional_usdt=Decimal("100"),
        idempotency_key=key,
        settings=ScannerSettings(),
    )
    repeated, repeated_created = await ledger.plan_paper_open(
        opportunity=opportunity,
        notional_usdt=Decimal("100"),
        idempotency_key=key,
        settings=ScannerSettings(),
    )

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert first.status == TradeIntentStatus.PLANNED
    assert first.base_quantity == Decimal("2000")
    assert {(leg.leg, leg.side) for leg in first.legs} == {
        ("spot", "buy"),
        ("perp", "sell"),
    }
    assert all(leg.client_order_id.startswith("bh-") for leg in first.legs)
    assert [row.id for row in await database.recoverable_trade_intents()] == [
        first.id
    ]

    with pytest.raises(IdempotencyConflict):
        await ledger.plan_paper_open(
            opportunity=opportunity,
            notional_usdt=Decimal("101"),
            idempotency_key=key,
            settings=ScannerSettings(),
        )
    await database.close()


async def test_trade_state_machine_uses_optimistic_versions() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )

    executing = await ledger.transition(
        intent_id=intent.id,
        expected_version=1,
        target=TradeIntentStatus.EXECUTING,
    )
    assert executing.version == 2
    with pytest.raises(StateConflict, match="version changed"):
        await ledger.transition(
            intent_id=intent.id,
            expected_version=1,
            target=TradeIntentStatus.HEDGED,
        )
    with pytest.raises(StateConflict, match="cannot transition"):
        await ledger.transition(
            intent_id=intent.id,
            expected_version=2,
            target=TradeIntentStatus.CLOSED,
        )
    await database.close()


async def test_paper_executor_atomically_fills_both_legs_and_opens_position() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    executor = PaperExecutionService(database)

    first = await executor.run_once()
    repeated = await executor.run_once()

    assert first.executed == 1
    assert repeated.executed == 0
    executed = await ledger.get(intent.id)
    assert executed is not None
    assert executed.status == TradeIntentStatus.HEDGED
    assert executed.version == 2
    assert all(leg.status == "filled" for leg in executed.legs)
    fills = await ledger.fills(intent.id)
    assert len(fills) == 2
    positions = await ledger.positions(status="open")
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("2000")
    assert positions[0].opening_fees_usdt.quantize(Decimal("0.001")) == Decimal(
        "0.151"
    )

    close_key = uuid.uuid4()
    close_intent, close_created = await ledger.plan_paper_close(
        position_id=positions[0].id,
        opportunity=_opportunity(),
        idempotency_key=close_key,
        settings=ScannerSettings(),
    )
    close_repeated, repeated_created = await ledger.plan_paper_close(
        position_id=positions[0].id,
        opportunity=_opportunity(),
        idempotency_key=close_key,
        settings=ScannerSettings(),
    )
    assert close_created is True
    assert repeated_created is False
    assert close_repeated.id == close_intent.id
    assert {(leg.leg, leg.side, leg.reduce_only) for leg in close_intent.legs} == {
        ("spot", "sell", False),
        ("perp", "buy", True),
    }

    closed_result = await executor.run_once()
    closed_position = (await ledger.positions(status="closed"))[0]
    assert closed_result.executed == 1
    assert closed_position.closing_intent_id == close_intent.id
    assert closed_position.closing_fees_usdt is not None
    assert closed_position.closing_fees_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("0.150")
    assert closed_position.realized_pnl_usdt is not None
    assert closed_position.realized_pnl_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("-4.301")
    await database.close()


async def test_paper_plan_rejects_stale_or_oversized_market_data() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    with pytest.raises(TradeValidationError, match="stale"):
        await ledger.plan_paper_open(
            opportunity=_opportunity(
                observed_at=datetime.now(UTC) - timedelta(seconds=16)
            ),
            notional_usdt=Decimal("100"),
            idempotency_key=uuid.uuid4(),
            settings=ScannerSettings(),
        )
    with pytest.raises(TradeValidationError, match="capacity"):
        await ledger.plan_paper_open(
            opportunity=_opportunity(),
            notional_usdt=Decimal("501"),
            idempotency_key=uuid.uuid4(),
            settings=ScannerSettings(),
        )
    await database.close()
