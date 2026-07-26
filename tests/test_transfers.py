import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from basis_hawk.accounts import (
    AccountSnapshot,
    InternalTransferSubmission,
    PositionMode,
    RemoteInternalTransfer,
    RemoteTradingState,
)
from basis_hawk.credentials import (
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.storage import Database
from basis_hawk.transfers import (
    InternalTransferDirection,
    InternalTransferExecutionService,
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


class _TransferCredentials:
    async def load(
        self,
        exchange: Exchange,
        environment: ExchangeEnvironment,
    ) -> ExchangeSecrets | None:
        del exchange, environment
        return ExchangeSecrets(api_key="key", api_secret="secret")


class _TransferClient:
    def __init__(self) -> None:
        self.submissions = 0
        self.snapshots = 0

    async def snapshot(self) -> AccountSnapshot:
        self.snapshots += 1
        before = self.snapshots == 1
        return AccountSnapshot(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal("100") if before else Decimal("50"),
            perp_usdt_available=Decimal("10") if before else Decimal("60"),
            perp_usdt_equity=Decimal("10") if before else Decimal("60"),
            shared_balance=False,
            account_mode="classic",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def trading_state(self) -> RemoteTradingState:
        return RemoteTradingState(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            open_orders=[],
            positions=[],
            complete=True,
        )

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        assert transfer_id
        assert direction == "spot_to_perp"
        assert amount == Decimal("50")
        self.submissions += 1
        return InternalTransferSubmission(
            transfer_id="exchange-transfer-id",
            status="pending",
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        del client_transfer_id, created_at
        return RemoteInternalTransfer(
            transfer_id=transfer_id,
            status="completed",
            direction=direction,
            amount=amount,
        )

    async def close(self) -> None:
        return None


async def test_transfer_worker_submits_once_and_confirms_arrival() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = InternalTransferLedger(
        database,
        per_request_limit_usdt=Decimal("100"),
        daily_limit_usdt=Decimal("100"),
    )
    row, _ = await ledger.plan(
        InternalTransferRequest(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            direction=InternalTransferDirection.SPOT_TO_PERP,
            amount_usdt=Decimal("50"),
        ),
        idempotency_key=uuid.uuid4(),
        actor="admin",
    )
    client = _TransferClient()
    executor = InternalTransferExecutionService(
        database,
        _TransferCredentials(),  # type: ignore[arg-type]
        account_client_factory=lambda *_: client,  # type: ignore[arg-type]
    )

    submitted = await executor.run_once()
    assert submitted.action == "submitted"
    assert submitted.status == "pending"
    assert client.submissions == 1

    completed = await executor.run_once()
    assert completed.action == "completed"
    assert completed.status == "completed"
    assert client.submissions == 1
    persisted = (await database.list_internal_transfers())[0]
    assert persisted.id == row.id
    assert persisted.source_balance_before == Decimal("100")
    assert persisted.target_balance_before == Decimal("10")
    assert persisted.expected_target_balance == Decimal("60")
    assert persisted.exchange_transfer_id == "exchange-transfer-id"
    assert persisted.completed_at is not None
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
    await database.close()


class _GateRecoveryClient:
    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        del client_transfer_id, created_at
        assert transfer_id == ""
        return RemoteInternalTransfer(
            transfer_id="recovered-gate-id",
            status="completed",
            direction=direction,
            amount=amount,
        )

    async def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            exchange=Exchange.GATE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal("5"),
            perp_usdt_available=Decimal("25"),
            perp_usdt_equity=Decimal("25"),
            shared_balance=False,
            account_mode="classic",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def close(self) -> None:
        return None


async def test_gate_transfer_recovers_missing_ack_by_client_id() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    ledger = InternalTransferLedger(
        database,
        per_request_limit_usdt=Decimal("10"),
        daily_limit_usdt=Decimal("10"),
    )
    row, _ = await ledger.plan(
        InternalTransferRequest(
            exchange=Exchange.GATE,
            environment=ExchangeEnvironment.LIVE,
            direction=InternalTransferDirection.SPOT_TO_PERP,
            amount_usdt=Decimal("5"),
        ),
        idempotency_key=uuid.uuid4(),
        actor="admin",
    )
    await database.prepare_internal_transfer_submission(
        transfer_id=row.id,
        source_balance=Decimal("10"),
        target_balance=Decimal("20"),
    )
    client = _GateRecoveryClient()
    executor = InternalTransferExecutionService(
        database,
        _TransferCredentials(),  # type: ignore[arg-type]
        account_client_factory=lambda *_: client,  # type: ignore[arg-type]
    )

    result = await executor.run_once()
    assert result.status == "completed"
    persisted = (await database.list_internal_transfers())[0]
    assert persisted.exchange_transfer_id == "recovered-gate-id"
    assert persisted.status == "completed"
    await database.close()
