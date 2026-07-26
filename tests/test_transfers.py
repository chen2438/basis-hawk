import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.storage import Database
from basis_hawk.transfers import (
    InternalTransferDirection,
    InternalTransferLedger,
    InternalTransferRequest,
)


async def test_transfer_ledger_is_idempotent_and_pauses_execution() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.set_execution_control(state="ready", reason="healthy")
    ledger = InternalTransferLedger(
        database,
        per_request_limit_usdt=Decimal("100"),
        daily_limit_usdt=Decimal("200"),
    )
    request = InternalTransferRequest(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        direction=InternalTransferDirection.SPOT_TO_PERP,
        amount_usdt=Decimal("50"),
    )
    key = uuid.uuid4()

    first, created = await ledger.plan(
        request,
        idempotency_key=key,
        actor="admin",
    )
    repeated, repeated_created = await ledger.plan(
        request,
        idempotency_key=key,
        actor="admin",
    )

    assert created
    assert not repeated_created
    assert repeated.id == first.id
    assert first.asset == "USDT"
    assert first.status == "planned"
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
    assert control.reason == "internal account transfer requires balance confirmation"

    conflicting = request.model_copy(update={"amount_usdt": Decimal("51")})
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        await ledger.plan(
            conflicting,
            idempotency_key=key,
            actor="admin",
        )
    await database.close()


async def test_transfer_limits_default_to_disabled_and_are_bounded() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    request = InternalTransferRequest(
        exchange=Exchange.GATE,
        environment=ExchangeEnvironment.LIVE,
        direction=InternalTransferDirection.PERP_TO_SPOT,
        amount_usdt=Decimal("1"),
    )
    disabled = InternalTransferLedger(
        database,
        per_request_limit_usdt=Decimal("0"),
        daily_limit_usdt=Decimal("0"),
    )
    with pytest.raises(ValueError, match="disabled by zero limits"):
        await disabled.plan(
            request,
            idempotency_key=uuid.uuid4(),
            actor="admin",
        )

    bounded = InternalTransferLedger(
        database,
        per_request_limit_usdt=Decimal("10"),
        daily_limit_usdt=Decimal("10"),
    )
    with pytest.raises(ValueError, match="per-request limit"):
        await bounded.plan(
            request.model_copy(update={"amount_usdt": Decimal("11")}),
            idempotency_key=uuid.uuid4(),
            actor="admin",
        )
    await database.close()


async def test_transfer_daily_limit_uses_utc_day_and_counts_pending_money() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime(2026, 7, 26, 23, 59, tzinfo=UTC)

    await database.plan_internal_transfer(
        transfer_id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        request_fingerprint="a" * 64,
        exchange="mexc",
        environment="live",
        direction="spot_to_perp",
        amount=Decimal("70"),
        per_request_limit=Decimal("100"),
        daily_limit=Decimal("100"),
        actor="admin",
        now=now,
    )
    with pytest.raises(ValueError, match="UTC daily limit"):
        await database.plan_internal_transfer(
            transfer_id=str(uuid.uuid4()),
            idempotency_key=str(uuid.uuid4()),
            request_fingerprint="b" * 64,
            exchange="mexc",
            environment="live",
            direction="spot_to_perp",
            amount=Decimal("31"),
            per_request_limit=Decimal("100"),
            daily_limit=Decimal("100"),
            actor="admin",
            now=now,
        )
    next_day, created = await database.plan_internal_transfer(
        transfer_id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        request_fingerprint="c" * 64,
        exchange="mexc",
        environment="live",
        direction="perp_to_spot",
        amount=Decimal("100"),
        per_request_limit=Decimal("100"),
        daily_limit=Decimal("100"),
        actor="admin",
        now=now + timedelta(minutes=1),
    )
    assert created
    assert next_day.amount == Decimal("100")
    assert len(await database.list_internal_transfers()) == 2
    await database.close()
