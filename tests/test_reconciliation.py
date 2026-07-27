import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from basis_hawk.accounts import (
    AccountSnapshot,
    OrderCancellation,
    PositionMode,
    PrivateRequestError,
    RemoteFill,
    RemoteFillBatch,
    RemoteOrder,
    RemoteOrderLookup,
    RemotePosition,
    RemoteTradingState,
)
from basis_hawk.automation import AutomaticTradingResult
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.private_stream import PrivateStreamRegistry
from basis_hawk.reconciliation import (
    ReconciliationService,
    _open_order_reasons,
    _position_reasons,
)
from basis_hawk.storage import (
    Database,
    OrderLegRow,
    RemoteOpenOrderSnapshotRow,
    RemotePositionSnapshotRow,
)


class FakeAccountClient:
    def __init__(
        self,
        *,
        fail: bool = False,
        fills: dict[str, list[RemoteFill]] | None = None,
        orders: dict[str, RemoteOrder] | None = None,
    ) -> None:
        self.fail = fail
        self.fills = fills or {}
        self.orders = orders or {}
        self.closed = False
        self.cancellations: list[str] = []

    async def snapshot(self) -> AccountSnapshot:
        if self.fail:
            raise PrivateRequestError("signed URL contained a sensitive value")
        return AccountSnapshot(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime(2026, 7, 26, 18, 0, tzinfo=UTC),
            spot_usdt_available=Decimal("100.25"),
            perp_usdt_available=Decimal("80.5"),
            perp_usdt_equity=Decimal("82"),
            shared_balance=False,
            account_mode="spot+usdt_futures",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def trading_state(self) -> RemoteTradingState:
        if self.fail:
            raise PrivateRequestError("signed URL contained a sensitive value")
        return RemoteTradingState(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime(2026, 7, 26, 18, 0, tzinfo=UTC),
            open_orders=[
                RemoteOrder(
                    exchange_order_id="1",
                    client_order_id="bh-test",
                    market="spot",
                    symbol="ORDERUSDT",
                    side="buy",
                    status="NEW",
                    price=Decimal("0.05"),
                    original_quantity=Decimal("10"),
                    filled_quantity=Decimal("0"),
                )
            ],
            positions=[
                RemotePosition(
                    symbol="ORDERUSDT",
                    side="short",
                    quantity=Decimal("10"),
                    entry_price=Decimal("0.051"),
                    mark_price=Decimal("0.05"),
                    liquidation_price=Decimal("0.09"),
                    leverage=Decimal("1"),
                    isolated=True,
                )
            ],
            complete=True,
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        return RemoteFillBatch(
            fills=self.fills.get(client_order_id or "", []),
            complete=True,
        )

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        return RemoteOrderLookup(
            order=self.orders.get(client_order_id),
            complete=True,
        )

    async def close(self) -> None:
        self.closed = True

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        self.cancellations.append(exchange_order_id or client_order_id or "")
        return OrderCancellation(
            market=market,
            symbol=symbol,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            accepted=True,
        )


class EmptyFakeAccountClient(FakeAccountClient):
    async def trading_state(self) -> RemoteTradingState:
        return RemoteTradingState(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime(2026, 7, 26, 18, 0, tzinfo=UTC),
            open_orders=[],
            positions=[],
            complete=True,
        )


def test_remote_open_orders_and_positions_are_matched_exactly() -> None:
    now = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
    leg = OrderLegRow(
        id=str(uuid.uuid4()),
        trade_intent_id=str(uuid.uuid4()),
        leg="perp",
        market="perp",
        symbol="ORDER-USDT-SWAP",
        side="sell",
        client_order_id="bh-local-perp",
        exchange_order_id="remote-perp",
        status="acknowledged",
        quantity=Decimal("2"),
        base_multiplier=Decimal("10"),
        limit_price=Decimal("0.051"),
        filled_quantity=Decimal("0"),
        reduce_only=False,
        created_at=now,
        updated_at=now,
    )
    order = RemoteOrder(
        exchange_order_id="remote-perp",
        client_order_id="bh-local-perp",
        market="perp",
        symbol="ORDER-USDT-SWAP",
        side="sell",
        status="live",
        price=Decimal("0.051"),
        original_quantity=Decimal("2"),
        filled_quantity=Decimal("0"),
        reduce_only=False,
    )
    position = RemotePosition(
        symbol="ORDER-USDT-SWAP",
        side="short",
        quantity=Decimal("2"),
        entry_price=Decimal("0.051"),
        mark_price=Decimal("0.05"),
        liquidation_price=Decimal("0.09"),
        leverage=Decimal("3"),
        isolated=True,
    )

    assert _open_order_reasons([order], [leg]) == [
        "locally linked IOC orders are still open"
    ]
    assert _open_order_reasons(
        [order.model_copy(update={"original_quantity": Decimal("3")})],
        [leg],
    ) == ["remote open order conflicts with its local order leg"]
    assert _position_reasons(
        [position],
        [("ORDER-USDT-SWAP", Decimal("2"), 3)],
    ) == []
    assert _position_reasons(
        [position.model_copy(update={"isolated": False})],
        [("ORDER-USDT-SWAP", Decimal("2"), 3)],
    ) == ["remote short position conflicts with the local pair"]
    assert (
        _position_reasons(
            [position.model_copy(update={"isolated": False})],
            [("ORDER-USDT-SWAP", Decimal("2"), 3)],
            expected_isolated=False,
        )
        == []
    )


async def _credentials(database: Database) -> CredentialService:
    service = CredentialService(database, SecretCipher(SecretCipher.generate_key()))
    await service.save(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="primary",
        secrets=ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        actor="test",
    )
    return service


async def test_startup_reconciliation_persists_snapshot_but_keeps_execution_blocked() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    client = FakeAccountClient()
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await reconciler.run_once()

    assert result.accounts_checked == 1
    assert result.accounts_blocked == 1
    assert result.accounts_failed == 0
    assert result.execution_state == "blocked"
    assert client.closed is True
    control = await database.execution_control()
    assert control is not None
    assert control.state == "blocked"
    states = await database.reconciliation_states()
    assert len(states) == 1
    assert states[0].status == "blocked"
    assert states[0].snapshot_id is not None
    assert states[0].trading_state_complete is True
    assert states[0].private_stream_ready is False
    assert states[0].open_order_count == 1
    assert states[0].position_count == 1
    assert "local intent" in states[0].reason
    async with database.sessions() as session:
        assert (await session.scalar(select(func.count(RemoteOpenOrderSnapshotRow.id)))) == 1
        assert (await session.scalar(select(func.count(RemotePositionSnapshotRow.id)))) == 1
    await database.close()


async def test_reconciliation_enters_ready_only_with_fresh_private_stream() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    await PrivateStreamRegistry(database).connected(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        orders_subscribed=True,
        fills_subscribed=True,
        positions_subscribed=True,
    )
    client = EmptyFakeAccountClient()

    class FakeAutomaticTrader:
        calls = 0

        async def run_once(self) -> AutomaticTradingResult:
            self.calls += 1
            return AutomaticTradingResult(
                evaluated=True,
                created=True,
                intent_id=str(uuid.uuid4()),
                action="open",
                reason="test",
            )

    automatic_trader = FakeAutomaticTrader()
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
        automatic_trader=automatic_trader,  # type: ignore[arg-type]
    )

    result = await reconciler.run_once()

    assert result.accounts_checked == 1
    assert result.accounts_blocked == 0
    assert result.accounts_failed == 0
    assert result.execution_state == "ready"
    control = await database.execution_control()
    assert control is not None
    assert control.state == "ready"
    states = await database.reconciliation_states()
    assert states[0].status == "ready"
    assert states[0].reason == "account reconciliation passed"
    assert states[0].private_stream_ready is True
    assert automatic_trader.calls == 1
    assert reconciler._reconciliation_requested.is_set()
    await database.close()


async def test_reconciliation_failure_is_persisted_without_exception_details() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    client = FakeAccountClient(fail=True)
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await reconciler.run_once()

    assert result.accounts_failed == 1
    states = await database.reconciliation_states()
    assert states[0].status == "error"
    assert states[0].reason == "private account reconciliation failed"
    assert "sensitive" not in states[0].reason
    assert client.closed is True
    await database.close()


async def test_reconciliation_persists_remote_fills_idempotently() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    intent_id = str(uuid.uuid4())
    now = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
    _, legs, _ = await database.create_trade_intent(
        intent={
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "a" * 64,
            "exchange": "binance",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "open",
            "status": "executing",
            "requested_notional": Decimal("1"),
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
                "trade_intent_id": intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDERUSDT",
                "side": "buy",
                "client_order_id": "bh-live-s",
                "exchange_order_id": "remote-1",
                "status": "acknowledged",
                "quantity": Decimal("20"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    remote_fill = RemoteFill(
        exchange_trade_id="trade-1",
        exchange_order_id="remote-1",
        client_order_id="bh-live-s",
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        quantity=Decimal("20"),
        price=Decimal("0.049"),
        fee_amount=Decimal("0.001"),
        fee_asset="ORDER",
        liquidity="taker",
        occurred_at=now,
    )
    client = FakeAccountClient(fills={"bh-live-s": [remote_fill]})
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    await reconciler.run_once()
    await reconciler.run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[1][0].status == "filled"
    assert stored[1][0].filled_quantity == Decimal("20")
    assert stored[1][0].average_price is not None
    assert stored[1][0].average_price.quantize(Decimal("0.001")) == Decimal(
        "0.049"
    )
    fills = await database.fills_for_intent(intent_id)
    assert len(fills) == 1
    assert fills[0].exchange_trade_id == "trade-1"
    states = await database.reconciliation_states()
    assert states[0].fill_reconciliation_complete is True
    assert states[0].fill_count == 1
    await database.close()


async def test_reconciliation_refreshes_linked_ioc_and_preserves_partial_terminal_state() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    intent_id = str(uuid.uuid4())
    now = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
    _, legs, _ = await database.create_trade_intent(
        intent={
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "1" * 64,
            "exchange": "binance",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "open",
            "status": "executing",
            "requested_notional": Decimal("1"),
            "base_quantity": Decimal("20"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "2" * 64,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        },
        legs=[
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDERUSDT",
                "side": "buy",
                "client_order_id": "bh-partial-ioc",
                "exchange_order_id": "remote-partial",
                "status": "acknowledged",
                "quantity": Decimal("20"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    remote_order = RemoteOrder(
        exchange_order_id="remote-partial",
        client_order_id="bh-partial-ioc",
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        status="EXPIRED",
        price=Decimal("0.05"),
        original_quantity=Decimal("20"),
        filled_quantity=Decimal("8"),
    )
    remote_fill = RemoteFill(
        exchange_trade_id="trade-partial",
        exchange_order_id="remote-partial",
        client_order_id="bh-partial-ioc",
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        quantity=Decimal("8"),
        price=Decimal("0.049"),
        fee_amount=Decimal("0.001"),
        fee_asset="ORDER",
        liquidity="taker",
        occurred_at=now,
    )
    client = FakeAccountClient(
        orders={"bh-partial-ioc": remote_order},
        fills={"bh-partial-ioc": [remote_fill]},
    )
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    await reconciler.run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[1][0].id == legs[0].id
    assert stored[1][0].status == "canceled"
    assert stored[1][0].filled_quantity == Decimal("8")
    assert stored[1][0].average_price is not None
    assert stored[1][0].average_price.quantize(Decimal("0.001")) == Decimal(
        "0.049"
    )
    states = await database.reconciliation_states()
    assert states[0].order_reconciliation_complete is True
    assert states[0].recovered_order_count == 0
    await database.close()


async def test_reconciliation_recovers_ack_lost_order_before_querying_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    intent_id = str(uuid.uuid4())
    now = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
    _, legs, _ = await database.create_trade_intent(
        intent={
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "c" * 64,
            "exchange": "binance",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "open",
            "status": "executing",
            "requested_notional": Decimal("1"),
            "base_quantity": Decimal("20"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "d" * 64,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        },
        legs=[
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDERUSDT",
                "side": "buy",
                "client_order_id": "bh-ack-lost-s",
                "exchange_order_id": None,
                "status": "submitted",
                "quantity": Decimal("20"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    remote_order = RemoteOrder(
        exchange_order_id="remote-ack-1",
        client_order_id="bh-ack-lost-s",
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        status="FILLED",
        price=Decimal("0.05"),
        original_quantity=Decimal("20"),
        filled_quantity=Decimal("20"),
    )
    remote_fill = RemoteFill(
        exchange_trade_id="trade-ack-1",
        exchange_order_id="remote-ack-1",
        client_order_id="bh-ack-lost-s",
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        quantity=Decimal("20"),
        price=Decimal("0.049"),
        fee_amount=Decimal("0.001"),
        fee_asset="ORDER",
        liquidity="taker",
        occurred_at=now,
    )
    client = FakeAccountClient(
        orders={"bh-ack-lost-s": remote_order},
        fills={"bh-ack-lost-s": [remote_fill]},
    )
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    await reconciler.run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[1][0].exchange_order_id == "remote-ack-1"
    assert stored[1][0].status == "filled"
    assert stored[1][0].filled_quantity == Decimal("20")
    states = await database.reconciliation_states()
    assert states[0].order_reconciliation_complete is True
    assert states[0].recovered_order_count == 1
    assert states[0].fill_reconciliation_complete is True
    assert states[0].fill_count == 1
    with pytest.raises(ValueError, match="client ID"):
        await database.reconcile_remote_order(
            order_leg_id=legs[0].id,
            order=remote_order.model_copy(
                update={"client_order_id": "different-client-id"}
            ),
        )
    await database.close()


async def test_reconciliation_blocks_when_submitted_order_cannot_be_found() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    intent_id = str(uuid.uuid4())
    now = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)
    await database.create_trade_intent(
        intent={
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "e" * 64,
            "exchange": "binance",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "open",
            "status": "executing",
            "requested_notional": Decimal("1"),
            "base_quantity": Decimal("20"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "f" * 64,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        },
        legs=[
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDERUSDT",
                "side": "buy",
                "client_order_id": "bh-missing-s",
                "exchange_order_id": None,
                "status": "submitted",
                "quantity": Decimal("20"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    client = FakeAccountClient()
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    await reconciler.run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[1][0].exchange_order_id is None
    assert stored[1][0].status == "submitted"
    states = await database.reconciliation_states()
    assert states[0].order_reconciliation_complete is False
    assert states[0].fill_reconciliation_complete is False
    assert "not found by client order ID" in states[0].reason
    await database.close()


async def test_sqlite_executor_lock_is_available_for_tests() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    async with database.executor_lock() as acquired:
        assert acquired is True
    await database.close()


async def test_private_event_wakes_serial_reconciliation_loop() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    reconciler = ReconciliationService(
        database,
        credentials,
        event_debounce_seconds=0,
    )
    calls = 0
    first = asyncio.Event()
    second = asyncio.Event()

    async def run_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first.set()
        elif calls == 2:
            second.set()

    reconciler.run_once = run_once  # type: ignore[method-assign]
    task = asyncio.create_task(
        reconciler.run_forever(interval_seconds=3_600)
    )
    await asyncio.wait_for(first.wait(), timeout=1)

    reconciler.request_reconciliation()
    reconciler.request_reconciliation()
    reconciler.request_reconciliation()
    await asyncio.wait_for(second.wait(), timeout=1)
    await asyncio.sleep(0.01)

    assert calls == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.close()


async def test_reconciliation_loop_invokes_live_executor() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )

    class FakeLiveExecutor:
        calls = 0

        async def run_once(self) -> None:
            self.calls += 1

    live_executor = FakeLiveExecutor()
    result = await ReconciliationService(
        database,
        credentials,
        live_executor=live_executor,  # type: ignore[arg-type]
    ).run_once()

    assert live_executor.calls == 1
    assert result.execution_state == "blocked"
    await database.close()


async def test_reconciliation_does_not_clear_a_safety_pause() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    reason = "paired trade compensation failed; manual exposure review is required"
    await database.set_execution_control(state="paused", reason=reason)
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )

    result = await ReconciliationService(database, credentials).run_once()

    assert result.execution_state == "paused"
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert control.reason == reason
    await database.close()


async def test_safety_pause_cancels_remote_orders_and_stays_paused() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    reason = "operator requested a safety pause"
    await database.set_execution_control(state="paused", reason=reason)
    credentials = await _credentials(database)
    client = FakeAccountClient()

    result = await ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    assert result.execution_state == "paused"
    assert client.cancellations == ["1"]
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert control.reason == reason
    states = await database.reconciliation_states()
    assert "pause cancellation was submitted" in states[0].reason
    await database.close()
