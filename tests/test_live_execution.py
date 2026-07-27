import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.accounts import (
    AccountSnapshot,
    LimitIocOrder,
    OrderSubmission,
    PerpConfiguration,
    PositionMode,
    RemoteFill,
    RemotePosition,
    RemoteTradingState,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.live_execution import (
    LiveCompensationService,
    LiveExecutionService,
)
from basis_hawk.models import (
    Exchange,
    InstrumentPair,
    Opportunity,
    Quality,
    ScannerSettings,
)
from basis_hawk.storage import Database, PairedPositionRow, TradeIntentRow
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
        fail_configuration: bool = False,
        positions: list[RemotePosition] | None = None,
    ) -> None:
        self.database = database
        self.fail_market = fail_market
        self.fail_configuration = fail_configuration
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
        if self.fail_configuration:
            raise RuntimeError(
                "sensitive exchange response with signed request data"
            )
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
    *,
    emergency: bool = False,
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
        maximum_slippage=(
            Decimal("0.2") if emergency else Decimal("0.001")
        ),
        emergency=emergency,
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


async def test_live_executor_expires_stale_open_without_remote_calls() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id = await _planned_live_intent(database)
    async with database.sessions() as session:
        intent = await session.get(TradeIntentRow, intent_id)
        assert intent is not None
        intent.market_observed_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(database)

    result = await LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert result.examined == 1
    assert result.submitted == 0
    assert stored[0].status == "failed"
    assert stored[0].failure_code == "market_data_expired"
    assert client.placed == []
    await database.close()


async def test_submission_transaction_rechecks_global_pause() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    _, intent_id = await _planned_live_intent(database)
    await database.set_execution_control(
        state="paused",
        reason="operator pause raced with preflight",
    )

    prepared = await database.prepare_live_submission(intent_id=intent_id)

    assert prepared is not None
    assert prepared[2] is False
    assert prepared[0].status == "planned"
    assert {item.status for item in prepared[1]} == {"created"}
    await database.close()


async def test_live_executor_expires_stale_close_and_reopens_position() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id, position_id = await _planned_live_close(database)
    async with database.sessions() as session:
        intent = await session.get(TradeIntentRow, intent_id)
        assert intent is not None
        intent.market_observed_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(database)

    result = await LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    stored = await database.trade_intent(intent_id)
    position = await database.paired_position(position_id)
    assert stored is not None and position is not None
    assert result.submitted == 0
    assert stored[0].status == "failed"
    assert stored[0].failure_code == "market_data_expired"
    assert position.status == "open"
    assert position.closing_intent_id is None
    assert client.placed == []
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


async def test_live_executor_submits_emergency_close_while_paused() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id, _ = await _planned_live_close(
        database,
        emergency=True,
    )
    await database.set_execution_control(
        state="paused",
        reason="emergency close confirmed",
    )
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

    result = await LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    stored = await database.trade_intent(intent_id)
    assert stored is not None
    assert result.submitted == 1
    assert stored[0].emergency is True
    assert stored[0].status == "executing"
    assert {item.reduce_only for item in client.placed} == {False, True}
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
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
    assert stored[0].failure_code == "close_state_mismatch"
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
    assert (
        control.reason
        == "live_order_preflight:okx:close_state_mismatch"
    )
    await database.close()


async def test_live_executor_persists_safe_configuration_failure() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id = await _planned_live_intent(database)
    await database.set_execution_control(state="ready", reason="test")
    client = FakeLiveAccountClient(database, fail_configuration=True)

    result = await LiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    stored = await database.trade_intent(intent_id)
    control = await database.execution_control()
    assert result.preflight_failed == 1
    assert client.placed == []
    assert stored is not None
    assert stored[0].status == "planned"
    assert stored[0].failure_code == "perp_configuration_failed"
    assert control is not None
    assert control.state == "paused"
    assert (
        control.reason
        == "live_order_preflight:okx:perp_configuration_failed"
    )
    assert "sensitive" not in control.reason
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


async def test_live_compensation_submits_once_with_fresh_protective_price() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials, intent_id = await _planned_live_intent(database)
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    primary = {item.leg: item for item in stored[1]}
    now = datetime.now(UTC)
    async with database.sessions() as session:
        intent = await session.get(TradeIntentRow, intent_id)
        assert intent is not None
        intent.status = "executing"
        for item in primary.values():
            leg = await session.get(type(item), item.id)
            assert leg is not None
            leg.status = "canceled"
        await session.commit()
    await database.persist_remote_fills(
        order_leg_id=primary["spot"].id,
        fills=[
            RemoteFill(
                exchange_trade_id="primary-spot",
                exchange_order_id="primary-spot-order",
                client_order_id=primary["spot"].client_order_id,
                market="spot",
                symbol=primary["spot"].symbol,
                side="buy",
                quantity=primary["spot"].quantity,
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=primary["perp"].id,
        fills=[
            RemoteFill(
                exchange_trade_id="primary-perp",
                exchange_order_id="primary-perp-order",
                client_order_id=primary["perp"].client_order_id,
                market="perp",
                symbol=primary["perp"].symbol,
                side="sell",
                quantity=primary["perp"].quantity / Decimal("2"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    first_settlement = await database.settle_live_open(
        intent_id=intent_id
    )
    assert first_settlement is not None
    assert first_settlement[0].status == "compensating"
    await database.save_latest_opportunities([_opportunity()])
    await database.replace_instruments("okx", [_pair()])

    class FakeCompensationClient(FakeLiveAccountClient):
        async def place_limit_ioc(
            self,
            order: LimitIocOrder,
        ) -> OrderSubmission:
            self.placed.append(order)
            return OrderSubmission(
                market=order.market,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id="remote-compensation",
            )

    client = FakeCompensationClient(database)
    service = LiveCompensationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    first = await service.run_once()
    repeated = await LiveCompensationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    ).run_once()

    assert first.submitted == 1
    assert first.uncertain == 0
    assert repeated.submitted == 0
    assert len(client.placed) == 1
    assert client.placed[0].market == "spot"
    assert client.placed[0].side == "sell"
    assert client.placed[0].limit_price < _opportunity().spot_bid
    current = await database.trade_intent(intent_id)
    assert current is not None
    compensation = next(
        item for item in current[1] if item.leg == "spot_compensation"
    )
    assert compensation.status == "acknowledged"
    await database.close()


async def test_gate_sandbox_compensation_uses_testnet_rules_and_depth() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    opportunity = _opportunity().model_copy(
        update={
            "exchange": Exchange.GATE,
            "spot_symbol": "ORDER_USDT",
            "perp_symbol": "ORDER_USDT",
        }
    )
    sandbox_pair = _pair().model_copy(
        update={
            "exchange": Exchange.GATE,
            "spot_symbol": "ORDER_USDT",
            "perp_symbol": "ORDER_USDT",
            "spot_price_increment": Decimal("0.001"),
            "spot_quantity_increment": Decimal("0.4"),
            "perp_price_increment": Decimal("0.0001"),
            "perp_contract_size": Decimal("0.01"),
        }
    )

    class FakeGateDepthAdapter:
        def __init__(self) -> None:
            self.closed = False

        async def instruments(self) -> list[InstrumentPair]:
            return [sandbox_pair]

        async def executable_quote(self, pair, quote):
            assert pair is sandbox_pair
            return quote.model_copy(
                update={
                    "observed_at": datetime.now(UTC),
                    "spot_bid": Decimal("0.048"),
                    "spot_bid_qty": Decimal("200"),
                    "spot_ask": Decimal("0.049"),
                    "spot_ask_qty": Decimal("150"),
                    "perp_bid": Decimal("0.050"),
                    "perp_bid_qty": Decimal("180"),
                    "perp_ask": Decimal("0.051"),
                    "perp_ask_qty": Decimal("170"),
                }
            )

        async def close(self) -> None:
            self.closed = True

    adapter = FakeGateDepthAdapter()
    environments: list[ExchangeEnvironment] = []

    def gate_adapter_factory(environment: ExchangeEnvironment):
        environments.append(environment)
        return adapter

    refreshed, resolved_pair = await LiveCompensationService(
        database,
        credentials,
        gate_adapter_factory=gate_adapter_factory,
    )._gate_sandbox_market(opportunity)

    assert environments == [ExchangeEnvironment.SANDBOX]
    assert resolved_pair is sandbox_pair
    assert refreshed.spot_bid == Decimal("0.048")
    assert refreshed.perp_ask == Decimal("0.051")
    assert refreshed.top_book_notional == Decimal("7.35")
    assert refreshed.close_top_book_notional == Decimal("8.67")
    assert adapter.closed is True
    await database.close()
