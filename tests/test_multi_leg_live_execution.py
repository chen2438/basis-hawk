from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from basis_hawk.accounts import (
    AccountSnapshot,
    OrderCancellation,
    OrderMode,
    OrderSubmission,
    PerpConfiguration,
    PerpMarginMode,
    PositionMode,
    RemoteFill,
    RemoteFillBatch,
    RemoteOrder,
    RemoteOrderLookup,
    RemoteTradingState,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.execution_tasks import ExecutionTaskService
from basis_hawk.models import Exchange
from basis_hawk.multi_leg import ExecutionTaskSpec
from basis_hawk.multi_leg_live_execution import (
    LiveOrderQuote,
    MultiLegLiveExecutionService,
    ResolvedLegMarket,
    _maker_is_outside_book_level,
)
from basis_hawk.order_books import OrderBookSnapshot
from basis_hawk.storage import Database, ExecutionOrderRow


def _live_spec(account_id: str) -> ExecutionTaskSpec:
    return ExecutionTaskSpec.model_validate(
        {
            "name": "maker chase live",
            "display_symbol": "BTC/USDT",
            "environment": "live",
            "base_asset": "BTC",
            "quantity_mode": "base",
            "maximum_base_exposure": "0.001",
            "maximum_notional_exposure_usdt": "100",
            "legs": [
                {
                    "account_id": account_id,
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
                        "maximum_chases": 2,
                        "fallback_mode": "protected_ioc",
                    },
                },
                {
                    "account_id": account_id,
                    "exchange": "binance",
                    "role": "hedge",
                    "market_type": "perpetual",
                    "side": "sell",
                    "base_asset": "BTC",
                    "symbol": "BTCUSDT",
                    "target_quantity": "0.01",
                    "order_mode": "protected_ioc",
                    "margin_mode": "isolated",
                    "leverage": 2,
                },
            ],
        }
    )


class FakeOrderBooks:
    async def fetch(
        self,
        *,
        exchange,
        environment,
        market,
        symbol,
        level,
    ) -> OrderBookSnapshot:
        assert exchange == Exchange.BINANCE
        assert environment == ExchangeEnvironment.LIVE
        assert market in {"spot", "perp"}
        assert symbol == "BTCUSDT"
        assert level == 3
        return OrderBookSnapshot(
            bids=(Decimal("50000"), Decimal("49999"), Decimal("49998")),
            asks=(Decimal("50001"), Decimal("50002"), Decimal("50003")),
            observed_at=datetime.now(UTC),
        )


async def test_spot_sell_is_blocked_when_owned_inventory_is_insufficient() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    placed = False

    class FakeClient:
        async def snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                spot_usdt_available=Decimal("1000"),
                perp_usdt_available=Decimal("1000"),
                perp_usdt_equity=Decimal("1000"),
                shared_balance=False,
                account_mode="spot+perp",
                position_mode=PositionMode.ONE_WAY,
                trade_permission=True,
            )

        async def spot_asset_available(self, asset: str) -> Decimal:
            assert asset == "BTC"
            return Decimal("0.009")

        async def place_order(self, order) -> OrderSubmission:
            nonlocal placed
            placed = True
            raise AssertionError("inventory guard must run before placement")

    executor = MultiLegLiveExecutionService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: FakeClient(),
    )
    leg = SimpleNamespace(
        market_type="spot",
        base_asset="BTC",
        symbol="BTCUSDT",
    )
    order = SimpleNamespace(
        side="sell",
        quantity=Decimal("0.01"),
        base_multiplier=Decimal("1"),
    )
    with pytest.raises(ValueError, match="spot inventory is insufficient"):
        await executor._submit_order(FakeClient(), SimpleNamespace(), leg, order)
    assert placed is False
    await database.close()


async def test_live_executor_confirms_cancel_before_maker_chase_and_hedges() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    account = await credentials.create_account(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="main",
        secrets=ExchangeSecrets(api_key="key-value", api_secret="secret-value"),
        actor="admin",
    )
    await database.set_execution_control(state="ready", reason="test ready")
    remote_orders: dict[str, dict[str, object]] = {}
    placements: list[object] = []

    class FakeClient:
        async def snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                spot_usdt_available=Decimal("1000"),
                perp_usdt_available=Decimal("1000"),
                perp_usdt_equity=Decimal("1000"),
                shared_balance=False,
                account_mode="spot+perp",
                position_mode=PositionMode.ONE_WAY,
                trade_permission=True,
                perp_margin_mode=PerpMarginMode.ISOLATED,
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

        async def order_by_client_id(
            self,
            *,
            market,
            symbol,
            client_order_id,
        ) -> RemoteOrderLookup:
            item = remote_orders.get(client_order_id)
            if item is None:
                return RemoteOrderLookup(order=None, complete=True)
            return RemoteOrderLookup(
                order=RemoteOrder(
                    exchange_order_id=str(item["exchange_order_id"]),
                    client_order_id=client_order_id,
                    market=market,
                    symbol=symbol,
                    side=str(item["side"]),
                    status=str(item["status"]),
                    price=Decimal("50000"),
                    original_quantity=Decimal(str(item["quantity"])),
                    filled_quantity=(
                        Decimal(str(item["quantity"]))
                        if item["status"] == "FILLED"
                        else Decimal("0")
                    ),
                ),
                complete=True,
            )

        async def fills_for_order(
            self,
            *,
            market,
            symbol,
            exchange_order_id,
            client_order_id,
            since,
        ) -> RemoteFillBatch:
            item = remote_orders[client_order_id]
            fills = (
                [
                    RemoteFill(
                        exchange_trade_id=f"fill-{exchange_order_id}",
                        exchange_order_id=str(exchange_order_id),
                        client_order_id=client_order_id,
                        market=market,
                        symbol=symbol,
                        side=str(item["side"]),
                        quantity=Decimal(str(item["quantity"])),
                        price=Decimal("50000"),
                        fee_amount=Decimal("0.01"),
                        fee_asset="USDT",
                        liquidity=(
                            "maker" if str(item["mode"]) == "maker" else "taker"
                        ),
                        occurred_at=datetime.now(UTC),
                    )
                ]
                if item["status"] == "FILLED"
                else []
            )
            return RemoteFillBatch(fills=fills, complete=True)

        async def place_order(self, order) -> OrderSubmission:
            placements.append(order)
            exchange_order_id = f"remote-{len(placements)}"
            remote_orders[order.client_order_id] = {
                "exchange_order_id": exchange_order_id,
                "quantity": order.quantity,
                "side": order.side,
                "mode": order.mode.value,
                "status": "NEW" if len(placements) == 1 else "FILLED",
            }
            return OrderSubmission(
                market=order.market,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=exchange_order_id,
            )

        async def cancel_order(
            self,
            *,
            market,
            symbol,
            exchange_order_id,
            client_order_id,
        ) -> OrderCancellation:
            remote_orders[client_order_id]["status"] = "CANCELED"
            return OrderCancellation(
                market=market,
                symbol=symbol,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                accepted=True,
            )

        async def configure_perp(
            self,
            *,
            symbol,
            leverage,
            position_mode,
        ) -> PerpConfiguration:
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )

        async def close(self) -> None:
            return None

    def client_factory(exchange, secrets, environment):
        assert exchange == Exchange.BINANCE
        assert secrets.api_key == "key-value"
        assert environment == ExchangeEnvironment.LIVE
        return FakeClient()

    tasks = ExecutionTaskService(
        database,
        credentials,
        client_factory,
        order_books=FakeOrderBooks(),
    )
    task, _ = await tasks.create(
        spec=_live_spec(account.id),
        idempotency_key=uuid4(),
        actor="admin",
    )
    ready = await tasks.preflight(task_id=task.id, actor="admin")
    assert ready.preflight is not None
    assert ready.preflight["maker_books"][0]["price"] == "49998"
    await tasks.start(
        task_id=task.id,
        expected_version=ready.version,
        actor="admin",
    )
    maker_quote_count = 0

    class Quotes:
        async def resolve_leg(self, leg, quantity_mode):
            return ResolvedLegMarket(
                base_quantity=Decimal("0.01"),
                base_multiplier=Decimal("1"),
                reference_price=Decimal("50000"),
                observed_at=datetime.now(UTC),
            )

        async def quote_order(
            self,
            leg,
            *,
            base_quantity,
            mode,
            environment,
            side=None,
        ):
            nonlocal maker_quote_count
            assert environment == "live"
            if mode == "maker":
                maker_quote_count += 1
            return LiveOrderQuote(
                native_quantity=base_quantity,
                base_multiplier=Decimal("1"),
                limit_price=(
                    None
                    if mode == "market"
                    else Decimal("50001")
                    if mode == "maker" and maker_quote_count > 1
                    else Decimal("50000")
                ),
                observed_at=datetime.now(UTC),
            )

    executor = MultiLegLiveExecutionService(
        database,
        credentials,
        account_client_factory=client_factory,
        quote_provider=Quotes(),
        worker_id="live-test-worker",
    )
    for _ in range(20):
        await executor.run_once()
        current = await tasks.get(task.id)
        assert current is not None
        if current.status == "completed":
            break
    assert current.status == "completed"
    assert [item.mode.value for item in placements] == [
        "maker",
        "maker",
        "protected_ioc",
    ]
    assert placements[0].client_order_id != placements[1].client_order_id

    async with database.sessions() as session:
        orders = list(
            await session.scalars(
                select(ExecutionOrderRow).order_by(ExecutionOrderRow.created_at)
            )
        )
    assert [item.status for item in orders] == [
        "canceled",
        "filled",
        "filled",
    ]
    assert orders[1].parent_order_id == orders[0].id
    assert orders[1].chase_number == 1
    assert [item.side for item in orders] == ["buy", "buy", "sell"]
    assert all(item.purpose == "primary" for item in orders)
    await database.close()


async def test_live_executor_compensates_filled_leg_after_hedge_failure() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    account = await credentials.create_account(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="main",
        secrets=ExchangeSecrets(api_key="key-value", api_secret="secret-value"),
        actor="admin",
    )
    await database.set_execution_control(state="ready", reason="test ready")
    remote_orders: dict[str, object] = {}
    placements: list[object] = []

    class FakeClient:
        async def snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                spot_usdt_available=Decimal("1000"),
                perp_usdt_available=Decimal("1000"),
                perp_usdt_equity=Decimal("1000"),
                shared_balance=False,
                account_mode="spot+perp",
                position_mode=PositionMode.ONE_WAY,
                trade_permission=True,
                perp_margin_mode=PerpMarginMode.ISOLATED,
            )

        async def spot_asset_available(self, asset: str) -> Decimal:
            assert asset == "BTC"
            return Decimal("1")

        async def trading_state(self) -> RemoteTradingState:
            return RemoteTradingState(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                open_orders=[],
                positions=[],
                complete=True,
            )

        async def order_by_client_id(
            self,
            *,
            market,
            symbol,
            client_order_id,
        ) -> RemoteOrderLookup:
            order = remote_orders.get(client_order_id)
            return RemoteOrderLookup(order=order, complete=True)

        async def fills_for_order(
            self,
            *,
            market,
            symbol,
            exchange_order_id,
            client_order_id,
            since,
        ) -> RemoteFillBatch:
            del since
            order = remote_orders[client_order_id]
            return RemoteFillBatch(
                fills=[
                    RemoteFill(
                        exchange_trade_id=f"fill-{exchange_order_id}",
                        exchange_order_id=str(exchange_order_id),
                        client_order_id=client_order_id,
                        market=market,
                        symbol=symbol,
                        side=order.side,
                        quantity=order.original_quantity,
                        price=order.price,
                        fee_amount=Decimal("0.01"),
                        fee_asset="USDT",
                        liquidity="taker",
                        occurred_at=datetime.now(UTC),
                    )
                ],
                complete=True,
            )

        async def place_order(self, order) -> OrderSubmission:
            placements.append(order)
            exchange_order_id = f"remote-{len(placements)}"
            remote_orders[order.client_order_id] = RemoteOrder(
                exchange_order_id=exchange_order_id,
                client_order_id=order.client_order_id,
                market=order.market,
                symbol=order.symbol,
                side=order.side,
                status="FILLED",
                price=order.limit_price or Decimal("50000"),
                original_quantity=order.quantity,
                filled_quantity=order.quantity,
                reduce_only=order.reduce_only,
            )
            return OrderSubmission(
                market=order.market,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=exchange_order_id,
            )

        async def configure_perp(
            self,
            *,
            symbol,
            leverage,
            position_mode,
        ) -> PerpConfiguration:
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )

        async def close(self) -> None:
            return None

    def client_factory(exchange, secrets, environment):
        return FakeClient()

    spec = _live_spec(account.id)
    spec = spec.model_copy(
        update={
            "legs": [
                spec.legs[0].model_copy(
                    update={
                        "order_mode": OrderMode.PROTECTED_IOC,
                        "maker_policy": None,
                    }
                ),
                spec.legs[1],
            ]
        }
    )
    tasks = ExecutionTaskService(database, credentials, client_factory)
    task, _ = await tasks.create(
        spec=spec,
        idempotency_key=uuid4(),
        actor="admin",
    )
    ready = await tasks.preflight(task_id=task.id, actor="admin")
    await tasks.start(
        task_id=task.id,
        expected_version=ready.version,
        actor="admin",
    )
    hedge_quote_failed = False

    class Quotes:
        async def resolve_leg(self, leg, quantity_mode):
            if hedge_quote_failed and leg.role == "hedge":
                raise ValueError("simulated hedge market outage")
            return ResolvedLegMarket(
                base_quantity=Decimal("0.01"),
                base_multiplier=Decimal("1"),
                reference_price=Decimal("50000"),
                observed_at=datetime.now(UTC),
            )

        async def quote_order(
            self,
            leg,
            *,
            base_quantity,
            mode,
            environment,
            side=None,
        ):
            nonlocal hedge_quote_failed
            assert environment == "live"
            if leg.role == "hedge" and side is None:
                hedge_quote_failed = True
                raise ValueError("simulated hedge quote failure")
            return LiveOrderQuote(
                native_quantity=base_quantity,
                base_multiplier=Decimal("1"),
                limit_price=Decimal("50000"),
                observed_at=datetime.now(UTC),
            )

    executor = MultiLegLiveExecutionService(
        database,
        credentials,
        account_client_factory=client_factory,
        quote_provider=Quotes(),
        worker_id="compensation-test-worker",
    )
    for _ in range(20):
        await executor.run_once()
        current = await tasks.get(task.id)
        assert current is not None
        if current.status in {"failed", "manual_review"}:
            break

    assert current.status == "failed"
    assert [item.side for item in placements] == ["buy", "sell"]
    assert [item.reduce_only for item in placements] == [False, False]
    orders = await database.execution_orders_for_run(
        (await database.execution_task_activity(task.id))[0][0].id
    )
    assert [item.purpose for item in orders] == [
        "primary",
        "compensation",
    ]
    assert [item.side for item in orders] == ["buy", "sell"]
    assert all(item.status == "filled" for item in orders)
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    await database.close()


def test_maker_chase_only_when_price_falls_outside_configured_level() -> None:
    assert _maker_is_outside_book_level(
        side="buy",
        order_price=Decimal("99"),
        book_level_price=Decimal("100"),
    )
    assert not _maker_is_outside_book_level(
        side="buy",
        order_price=Decimal("101"),
        book_level_price=Decimal("100"),
    )
    assert _maker_is_outside_book_level(
        side="sell",
        order_price=Decimal("101"),
        book_level_price=Decimal("100"),
    )
    assert not _maker_is_outside_book_level(
        side="sell",
        order_price=Decimal("99"),
        book_level_price=Decimal("100"),
    )
