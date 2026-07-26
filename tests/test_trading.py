import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import (
    Exchange,
    InstrumentPair,
    Opportunity,
    Quality,
    ScannerSettings,
)
from basis_hawk.storage import Database
from basis_hawk.trading import (
    IdempotencyConflict,
    PaperExecutionService,
    StateConflict,
    TradeIntentStatus,
    TradeLedger,
    TradeValidationError,
    _live_client_order_ids,
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


def _pair(exchange: Exchange = Exchange.OKX) -> InstrumentPair:
    return InstrumentPair(
        exchange=exchange,
        base_asset="ORDER",
        spot_symbol=(
            "ORDER-USDT" if exchange in {Exchange.OKX, Exchange.GATE} else "ORDERUSDT"
        ),
        perp_symbol=(
            "ORDER-USDT-SWAP"
            if exchange == Exchange.OKX
            else "ORDER_USDT"
            if exchange == Exchange.GATE
            else "ORDERUSDT"
        ),
        spot_price_increment=Decimal("0.00001"),
        spot_quantity_increment=Decimal("0.1"),
        spot_min_quantity=Decimal("1"),
        spot_min_notional=Decimal("5"),
        perp_price_increment=Decimal("0.000001"),
        perp_quantity_increment=Decimal("1"),
        perp_min_quantity=Decimal("1"),
        perp_min_notional=Decimal("5"),
        perp_contract_size=Decimal("10"),
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
    assert all(leg.base_multiplier == Decimal("1") for leg in first.legs)
    assert all(leg.client_order_id.startswith("bh-") for leg in first.legs)
    assert [row.id for row in await database.recoverable_trade_intents()] == [first.id]

    with pytest.raises(IdempotencyConflict):
        await ledger.plan_paper_open(
            opportunity=opportunity,
            notional_usdt=Decimal("101"),
            idempotency_key=key,
            settings=ScannerSettings(),
        )
    await database.close()


async def test_live_open_plan_persists_native_sizes_limits_and_leverage() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    key = uuid.uuid4()
    opportunity = _opportunity().model_copy(
        update={
            "exchange": Exchange.OKX,
            "spot_symbol": "ORDER-USDT",
            "perp_symbol": "ORDER-USDT-SWAP",
        }
    )

    planned, created = await ledger.plan_live_open(
        opportunity=opportunity,
        pair=_pair(),
        notional_usdt=Decimal("100"),
        idempotency_key=key,
        settings=ScannerSettings(),
        environment=ExchangeEnvironment.LIVE,
        leverage=3,
        maximum_slippage=Decimal("0.001"),
    )
    repeated, repeated_created = await ledger.plan_live_open(
        opportunity=opportunity,
        pair=_pair(),
        notional_usdt=Decimal("100"),
        idempotency_key=key,
        settings=ScannerSettings(),
        environment=ExchangeEnvironment.LIVE,
        leverage=3,
    )

    assert created is True
    assert repeated_created is False
    assert repeated.id == planned.id
    assert planned.environment == "live"
    assert planned.leverage == 3
    assert planned.base_quantity == Decimal("2000")
    legs = {item.leg: item for item in planned.legs}
    assert legs["spot"].quantity == Decimal("2000")
    assert legs["spot"].base_multiplier == Decimal("1")
    assert legs["perp"].quantity == Decimal("200")
    assert legs["perp"].base_multiplier == Decimal("10")
    assert legs["spot"].quantity * legs["spot"].base_multiplier == (
        legs["perp"].quantity * legs["perp"].base_multiplier
    )
    assert legs["spot"].limit_price <= Decimal("0.05") * Decimal("1.001")
    assert legs["perp"].limit_price >= Decimal("0.051") * Decimal("0.999")
    assert all(re.fullmatch(r"[A-Za-z0-9]{1,32}", item.client_order_id) for item in planned.legs)
    await database.close()


def test_live_client_order_ids_satisfy_gate_and_generic_constraints() -> None:
    intent_id = "12345678-1234-1234-1234-123456789012"
    gate_ids = _live_client_order_ids(Exchange.GATE, intent_id)
    generic_ids = _live_client_order_ids(Exchange.BITGET, intent_id)

    assert all(
        value.startswith("t-") and len(value.encode()) <= 30
        for value in gate_ids
    )
    assert all(
        re.fullmatch(r"[.A-Za-z0-9_:/\\-]{1,32}", value)
        for value in generic_ids
    )


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


async def test_order_leg_rejects_non_positive_base_multiplier() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    opportunity = _opportunity()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=opportunity,
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    stored = await database.trade_intent(intent.id)
    assert stored is not None
    assert all(item.base_multiplier == Decimal("1") for item in stored[1])

    now = datetime.now(UTC)
    invalid_intent_id = str(uuid.uuid4())
    with pytest.raises(IntegrityError):
        await database.create_trade_intent(
            intent={
                "id": invalid_intent_id,
                "idempotency_key": str(uuid.uuid4()),
                "request_fingerprint": "a" * 64,
                "exchange": "okx",
                "environment": "live",
                "base_asset": "ORDER",
                "action": "open",
                "status": "planned",
                "requested_notional": Decimal("100"),
                "base_quantity": Decimal("20"),
                "spot_fee_rate": Decimal("0.001"),
                "perp_fee_rate": Decimal("0.0005"),
                "market_observed_at": now,
                "config_version": "b" * 64,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            },
            legs=[
                {
                    "id": str(uuid.uuid4()),
                    "trade_intent_id": invalid_intent_id,
                    "leg": "perp",
                    "market": "perp",
                    "symbol": "ORDER-USDT-SWAP",
                    "side": "sell",
                    "client_order_id": "bh-invalid-multiplier",
                    "status": "created",
                    "quantity": Decimal("2"),
                    "base_multiplier": Decimal("0"),
                    "limit_price": Decimal("0.05"),
                    "filled_quantity": Decimal("0"),
                    "reduce_only": False,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
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
    assert positions[0].opening_fees_usdt.quantize(Decimal("0.001")) == Decimal("0.151")

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
    assert closed_position.closing_fees_usdt.quantize(Decimal("0.001")) == Decimal("0.150")
    assert closed_position.realized_pnl_usdt is not None
    assert closed_position.realized_pnl_usdt.quantize(Decimal("0.001")) == Decimal("-4.301")
    await database.close()


async def test_paper_executor_compensates_excess_partial_fill() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    executor = PaperExecutionService(
        database,
        fill_ratios={"spot": Decimal("1"), "perp": Decimal("0.5")},
    )

    result = await executor.run_once()

    assert result.executed == 1
    assert result.compensated == 1
    assert result.manual_review == 0
    executed = await ledger.get(intent.id)
    assert executed is not None
    assert executed.status == TradeIntentStatus.HEDGED
    compensation = next(leg for leg in executed.legs if leg.leg == "spot_compensation")
    assert compensation.side == "sell"
    assert compensation.quantity == Decimal("1000")
    assert compensation.status == "filled"
    assert compensation.reduce_only is False
    position = (await ledger.positions(status="open"))[0]
    assert position.quantity == Decimal("1000")
    assert position.opening_fees_usdt.quantize(Decimal("0.0001")) == Decimal("0.1755")
    assert len(await ledger.fills(intent.id)) == 3
    assert (await executor.run_once()).executed == 0
    await database.close()


async def test_paper_compensation_recovers_after_worker_restart() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    recorded = await database.record_paper_open_fills(
        intent_id=intent.id,
        spot_fill_quantity=Decimal("500"),
        perp_fill_quantity=Decimal("2000"),
    )
    assert recorded is not None
    assert recorded[0].status == TradeIntentStatus.COMPENSATING

    restarted = PaperExecutionService(database)
    result = await restarted.run_once()

    assert result.executed == 1
    assert result.compensated == 1
    executed = await ledger.get(intent.id)
    assert executed is not None
    compensation = next(leg for leg in executed.legs if leg.leg == "perp_compensation")
    assert compensation.side == "buy"
    assert compensation.reduce_only is True
    assert compensation.status == "filled"
    assert (await ledger.positions(status="open"))[0].quantity == Decimal("500")
    await database.close()


async def test_failed_paper_compensation_pauses_execution_for_manual_review() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    executor = PaperExecutionService(
        database,
        fill_ratios={"spot": Decimal("1"), "perp": Decimal("0")},
        compensation_succeeds=False,
    )

    result = await executor.run_once()

    assert result.executed == 1
    assert result.compensated == 0
    assert result.manual_review == 1
    executed = await ledger.get(intent.id)
    assert executed is not None
    assert executed.status == TradeIntentStatus.MANUAL_REVIEW
    assert (await ledger.positions(status="open")) == []
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert "compensation failed" in control.reason
    await database.close()


async def test_successful_single_leg_compensation_leaves_no_exposure() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    intent, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    executor = PaperExecutionService(
        database,
        fill_ratios={"spot": Decimal("0"), "perp": Decimal("1")},
    )

    result = await executor.run_once()

    assert result.compensated == 1
    assert result.manual_review == 0
    executed = await ledger.get(intent.id)
    assert executed is not None
    assert executed.status == TradeIntentStatus.FAILED
    compensation = next(leg for leg in executed.legs if leg.leg == "perp_compensation")
    assert compensation.side == "buy"
    assert compensation.reduce_only is True
    assert compensation.status == "filled"
    assert await ledger.positions(status="open") == []
    await database.close()


async def test_partial_paper_close_compensates_and_allows_remaining_close() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    opening, _ = await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    assert (await PaperExecutionService(database).run_once()).executed == 1
    position = (await ledger.positions(status="open"))[0]
    assert position.opening_intent_id == opening.id
    assert position.initial_quantity == Decimal("2000")
    assert position.remaining_opening_fees_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("0.151")
    first_close, _ = await ledger.plan_paper_close(
        position_id=position.id,
        opportunity=_opportunity(),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )

    partial = PaperExecutionService(
        database,
        fill_ratios={"spot": Decimal("1"), "perp": Decimal("0.5")},
    )
    result = await partial.run_once()

    assert result.executed == 1
    assert result.compensated == 1
    first_closed = await ledger.get(first_close.id)
    assert first_closed is not None
    assert first_closed.status == TradeIntentStatus.CLOSED
    compensation = next(
        leg for leg in first_closed.legs if leg.leg == "spot_compensation"
    )
    assert compensation.side == "buy"
    assert compensation.reduce_only is False
    remaining = (await ledger.positions(status="open"))[0]
    assert remaining.id == position.id
    assert remaining.quantity == Decimal("1000")
    assert remaining.initial_quantity == Decimal("2000")
    assert remaining.closing_intent_id is None
    assert remaining.remaining_opening_fees_usdt.quantize(
        Decimal("0.0001")
    ) == Decimal("0.0755")
    assert remaining.closing_fees_usdt is not None
    assert remaining.closing_fees_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("0.173")
    assert remaining.realized_pnl_usdt is not None
    assert remaining.realized_pnl_usdt.quantize(
        Decimal("0.0001")
    ) == Decimal("-2.2485")

    final_close, created = await ledger.plan_paper_close(
        position_id=position.id,
        opportunity=_opportunity(),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    assert created is True
    assert final_close.id != first_close.id
    assert (await PaperExecutionService(database).run_once()).executed == 1
    closed = (await ledger.positions(status="closed"))[0]
    assert closed.quantity == Decimal("0")
    assert closed.remaining_opening_fees_usdt == Decimal("0")
    assert closed.realized_pnl_usdt is not None
    assert closed.realized_pnl_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("-4.399")
    assert (
        await database.daily_realized_pnl(
            environment="paper",
            exchanges={"binance"},
            since=datetime.now(UTC) - timedelta(minutes=1),
        )
    ).quantize(Decimal("0.001")) == Decimal("-4.399")
    assert await database.daily_realized_pnl(
        environment="paper",
        exchanges=set(),
        since=datetime.now(UTC) - timedelta(minutes=1),
    ) == Decimal("0")
    await database.close()


async def test_partial_close_compensation_recovers_after_restart() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    await PaperExecutionService(database).run_once()
    position = (await ledger.positions(status="open"))[0]
    close_intent, _ = await ledger.plan_paper_close(
        position_id=position.id,
        opportunity=_opportunity(),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    recorded = await database.record_paper_close_fills(
        intent_id=close_intent.id,
        spot_fill_quantity=Decimal("500"),
        perp_fill_quantity=Decimal("2000"),
    )
    assert recorded is not None
    assert recorded[0].status == TradeIntentStatus.COMPENSATING

    result = await PaperExecutionService(database).run_once()

    assert result.executed == 1
    assert result.compensated == 1
    recovered = await ledger.get(close_intent.id)
    assert recovered is not None
    compensation = next(
        leg for leg in recovered.legs if leg.leg == "perp_compensation"
    )
    assert compensation.side == "sell"
    assert compensation.reduce_only is False
    assert compensation.status == "filled"
    remaining = (await ledger.positions(status="open"))[0]
    assert remaining.quantity == Decimal("1500")
    await database.close()


async def test_failed_partial_close_compensation_keeps_position_for_review() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    await ledger.plan_paper_open(
        opportunity=_opportunity(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )
    await PaperExecutionService(database).run_once()
    position = (await ledger.positions(status="open"))[0]
    close_intent, _ = await ledger.plan_paper_close(
        position_id=position.id,
        opportunity=_opportunity(),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
    )

    result = await PaperExecutionService(
        database,
        fill_ratios={"spot": Decimal("1"), "perp": Decimal("0")},
        compensation_succeeds=False,
    ).run_once()

    assert result.manual_review == 1
    failed = await ledger.get(close_intent.id)
    assert failed is not None
    assert failed.status == TradeIntentStatus.MANUAL_REVIEW
    reviewing = (await ledger.positions(status="closing"))[0]
    assert reviewing.id == position.id
    assert reviewing.quantity == Decimal("2000")
    assert reviewing.closing_intent_id == close_intent.id
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    await database.close()


async def test_paper_plan_rejects_stale_or_oversized_market_data() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = TradeLedger(database)
    with pytest.raises(TradeValidationError, match="stale"):
        await ledger.plan_paper_open(
            opportunity=_opportunity(observed_at=datetime.now(UTC) - timedelta(seconds=16)),
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
