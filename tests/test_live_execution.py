import uuid
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.accounts import (
    AccountSnapshot,
    LimitIocOrder,
    OrderSubmission,
    PerpConfiguration,
    PositionMode,
    RemotePosition,
    RemoteTradingState,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.live_execution import LiveExecutionService
from basis_hawk.models import (
    Exchange,
    InstrumentPair,
    Opportunity,
    Quality,
    ScannerSettings,
)
from basis_hawk.storage import Database, PairedPositionRow
from basis_hawk.trading import TradeLedger


def _opportunity() -> Opportunity:
    return Opportunity(
        exchange=Exchange.OKX,
        base_asset="ORDER",
        spot_symbol="ORDER-USDT",
        perp_symbol="ORDER-USDT-SWAP",
        observed_at=datetime.now(UTC),
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


def _pair() -> InstrumentPair:
    return InstrumentPair(
        exchange=Exchange.OKX,
        base_asset="ORDER",
        spot_symbol="ORDER-USDT",
        perp_symbol="ORDER-USDT-SWAP",
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


class FakeLiveAccountClient:
    def __init__(
        self,
        database: Database,
        *,
        fail_market: str | None = None,
        positions: list[RemotePosition] | None = None,
    ) -> None:
        self.database = database
        self.fail_market = fail_market
        self.positions = positions or []
        self.placed: list[LimitIocOrder] = []
        self.configured: list[tuple[str, int, PositionMode]] = []
        self.closed = False

    async def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            exchange=Exchange.OKX,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal("1000"),
            perp_usdt_available=Decimal("1000"),
            perp_usdt_equity=Decimal("1000"),
            shared_balance=False,
            account_mode="single:isolated",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def trading_state(self) -> RemoteTradingState:
        return RemoteTradingState(
            exchange=Exchange.OKX,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime.now(UTC),
            open_orders=[],
            positions=self.positions,
            complete=True,
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        self.configured.append((symbol, leverage, position_mode))
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        recoverable = await self.database.recoverable_trade_intents()
        executing = next(
            item for item in recoverable if item.status == "executing"
        )
        current = await self.database.trade_intent(executing.id)
        assert current is not None
        assert current[0].status == "executing"
        assert {item.status for item in current[1]} == {"submitted"}
        self.placed.append(order)
        if order.market == self.fail_market:
            raise RuntimeError("simulated acknowledgement loss")
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=f"remote-{order.market}",
        )

    async def close(self) -> None:
        self.closed = True


async def _planned_live_intent(
    database: Database,
) -> tuple[CredentialService, str]:
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    await credentials.save(
        exchange=Exchange.OKX,
        environment=ExchangeEnvironment.LIVE,
        label="primary",
        secrets=ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
            passphrase="test-passphrase",
        ),
        actor="test",
    )
    intent, _ = await TradeLedger(database).plan_live_open(
        opportunity=_opportunity(),
        pair=_pair(),
        notional_usdt=Decimal("100"),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
        environment=ExchangeEnvironment.LIVE,
        leverage=2,
    )
    return credentials, intent.id


async def _planned_live_close(
    database: Database,
) -> tuple[CredentialService, str, str]:
    credentials, opening_intent_id = await _planned_live_intent(database)
    opening = await database.trade_intent(opening_intent_id)
    assert opening is not None
    now = datetime.now(UTC)
    position = PairedPositionRow(
        id=str(uuid.uuid4()),
        opening_intent_id=opening_intent_id,
        exchange=Exchange.OKX.value,
        environment=ExchangeEnvironment.LIVE.value,
        base_asset="ORDER",
        initial_quantity=opening[0].base_quantity,
        quantity=opening[0].base_quantity,
        spot_entry_price=Decimal("0.05"),
        perp_entry_price=Decimal("0.051"),
        opening_fees_usdt=Decimal("0.1"),
        remaining_opening_fees_usdt=Decimal("0.1"),
        status="open",
        opened_at=now,
    )
    async with database.sessions() as session:
        stored_opening = await session.get(type(opening[0]), opening_intent_id)
        assert stored_opening is not None
        stored_opening.status = "hedged"
        session.add(position)
        await session.commit()
    closing, _ = await TradeLedger(database).plan_live_close(
        position_id=position.id,
        opportunity=_opportunity(),
        pair=_pair(),
        idempotency_key=uuid.uuid4(),
        settings=ScannerSettings(),
        environment=ExchangeEnvironment.LIVE,
    )
    return credentials, closing.id, position.id


async def test_live_executor_persists_both_submissions_before_parallel_orders() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id = await _planned_live_intent(database)
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(database)
    executor = LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await executor.run_once()

    assert result.examined == 1
    assert result.submitted == 1
    assert result.uncertain == 0
    assert result.preflight_failed == 0
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[0].status == "executing"
    assert stored[0].version == 2
    assert {item.status for item in stored[1]} == {"acknowledged"}
    assert {item.exchange_order_id for item in stored[1]} == {
        "remote-spot",
        "remote-perp",
    }
    assert {item.market for item in client.placed} == {"spot", "perp"}
    assert client.configured == [
        ("ORDER-USDT-SWAP", 2, PositionMode.ONE_WAY)
    ]
    assert client.closed is True
    await database.close()


async def test_live_executor_submits_exact_reduce_only_close() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id, position_id = await _planned_live_close(database)
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(
        database,
        positions=[
            RemotePosition(
                symbol="ORDER-USDT-SWAP",
                side="short",
                quantity=Decimal("200"),
                entry_price=Decimal("0.051"),
                mark_price=Decimal("0.05"),
                liquidation_price=Decimal("0.09"),
                leverage=Decimal("2"),
                isolated=True,
            )
        ],
    )
    executor = LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await executor.run_once()

    assert result.submitted == 1
    assert result.preflight_failed == 0
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert stored[0].status == "executing"
    orders = {item.market: item for item in client.placed}
    assert orders["spot"].side == "sell"
    assert orders["spot"].reduce_only is False
    assert orders["perp"].side == "buy"
    assert orders["perp"].reduce_only is True
    assert orders["perp"].quantity == Decimal("200")
    assert client.configured == []
    position = await database.paired_position(position_id)
    assert position is not None and position.status == "closing"
    await database.close()


async def test_live_executor_rejects_close_when_remote_position_drifted() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id, _ = await _planned_live_close(database)
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(
        database,
        positions=[
            RemotePosition(
                symbol="ORDER-USDT-SWAP",
                side="short",
                quantity=Decimal("199"),
                entry_price=Decimal("0.051"),
                mark_price=Decimal("0.05"),
                liquidation_price=Decimal("0.09"),
                leverage=Decimal("2"),
                isolated=True,
            )
        ],
    )
    executor = LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await executor.run_once()

    assert result.submitted == 0
    assert result.preflight_failed == 1
    assert client.placed == []
    stored = await database.trade_intent(intent_id)
    assert stored is not None and stored[0].status == "planned"
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
    await database.close()


async def test_live_executor_marks_uncertain_leg_pauses_and_never_resubmits() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id = await _planned_live_intent(database)
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(database, fail_market="perp")
    executor = LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    first = await executor.run_once()
    repeated = await executor.run_once()

    assert first.submitted == 1
    assert first.uncertain == 1
    assert repeated.examined == 0
    assert len(client.placed) == 2
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    statuses = {item.market: item.status for item in stored[1]}
    assert statuses == {"spot": "acknowledged", "perp": "unknown"}
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert "client-order-ID reconciliation" in control.reason
    await database.close()
