import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.accounts import RemoteFill
from basis_hawk.storage import (
    Database,
    OrderLegRow,
    PairedPositionRow,
    TradeIntentRow,
)


async def _live_intent(
    database: Database,
    *,
    terminal_status: str = "acknowledged",
) -> tuple[str, dict[str, str], datetime]:
    intent_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    _, legs, _ = await database.create_trade_intent(
        intent={
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "a" * 64,
            "exchange": "okx",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "open",
            "status": "executing",
            "leverage": 2,
            "requested_notional": Decimal("1"),
            "base_quantity": Decimal("20"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "b" * 64,
            "version": 2,
            "created_at": now,
            "updated_at": now,
        },
        legs=[
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDER-USDT",
                "side": "buy",
                "client_order_id": "bhspot" + intent_id.replace("-", "")[:20],
                "exchange_order_id": "remote-spot",
                "status": terminal_status,
                "quantity": Decimal("20"),
                "base_multiplier": Decimal("1"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": "perp",
                "market": "perp",
                "symbol": "ORDER-USDT-SWAP",
                "side": "sell",
                "client_order_id": "bhperp" + intent_id.replace("-", "")[:20],
                "exchange_order_id": "remote-perp",
                "status": terminal_status,
                "quantity": Decimal("2"),
                "base_multiplier": Decimal("10"),
                "limit_price": Decimal("0.051"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    return intent_id, {item.market: item.id for item in legs}, now


async def _live_close_intent(
    database: Database,
    *,
    terminal_status: str = "canceled",
) -> tuple[str, str, dict[str, str], datetime]:
    opening_intent_id, _, now = await _live_intent(
        database,
        terminal_status="filled",
    )
    async with database.sessions() as session:
        opening = await session.get(TradeIntentRow, opening_intent_id)
        assert opening is not None
        opening.status = "hedged"
        position = PairedPositionRow(
            id=str(uuid.uuid4()),
            opening_intent_id=opening_intent_id,
            exchange="okx",
            environment="live",
            base_asset="ORDER",
            initial_quantity=Decimal("20"),
            quantity=Decimal("20"),
            spot_entry_price=Decimal("0.049"),
            perp_entry_price=Decimal("0.051"),
            opening_fees_usdt=Decimal("0.01"),
            remaining_opening_fees_usdt=Decimal("0.01"),
            status="open",
            opened_at=now,
        )
        session.add(position)
        await session.commit()
    closing_intent_id = str(uuid.uuid4())
    _, legs, _ = await database.create_paper_close_intent(
        position_id=position.id,
        intent={
            "id": closing_intent_id,
            "paired_position_id": position.id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "c" * 64,
            "exchange": "okx",
            "environment": "live",
            "base_asset": "ORDER",
            "action": "close",
            "status": "executing",
            "leverage": 2,
            "requested_notional": Decimal("1"),
            "base_quantity": Decimal("20"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "d" * 64,
            "version": 2,
            "created_at": now,
            "updated_at": now,
        },
        legs=[
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": closing_intent_id,
                "leg": "spot",
                "market": "spot",
                "symbol": "ORDER-USDT",
                "side": "sell",
                "client_order_id": (
                    "bhspot" + closing_intent_id.replace("-", "")[:20]
                ),
                "exchange_order_id": "close-spot",
                "status": terminal_status,
                "quantity": Decimal("20"),
                "base_multiplier": Decimal("1"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "trade_intent_id": closing_intent_id,
                "leg": "perp",
                "market": "perp",
                "symbol": "ORDER-USDT-SWAP",
                "side": "buy",
                "client_order_id": (
                    "bhperp" + closing_intent_id.replace("-", "")[:20]
                ),
                "exchange_order_id": "close-perp",
                "status": terminal_status,
                "quantity": Decimal("2"),
                "base_multiplier": Decimal("10"),
                "limit_price": Decimal("0.05"),
                "filled_quantity": Decimal("0"),
                "reduce_only": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    return (
        closing_intent_id,
        position.id,
        {item.market: item.id for item in legs},
        now,
    )


async def test_live_settlement_opens_position_from_equal_base_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, legs, now = await _live_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="spot-fill",
                exchange_order_id="remote-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="buy",
                quantity=Decimal("20"),
                price=Decimal("0.049"),
                fee_amount=Decimal("0.01"),
                fee_asset="ORDER",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=legs["perp"],
        fills=[
            RemoteFill(
                exchange_trade_id="perp-fill",
                exchange_order_id="remote-perp",
                client_order_id=None,
                market="perp",
                symbol="ORDER-USDT-SWAP",
                side="sell",
                quantity=Decimal("2"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0.002"),
                fee_asset="USDT",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_open(intent_id=intent_id)
    repeated = await database.settle_live_open(intent_id=intent_id)

    assert settled is not None
    assert settled[2] is True
    assert settled[0].status == "hedged"
    assert settled[1] is not None
    assert settled[1].quantity == Decimal("20")
    assert settled[1].spot_entry_price.quantize(Decimal("0.001")) == Decimal(
        "0.049"
    )
    assert settled[1].perp_entry_price.quantize(Decimal("0.001")) == Decimal(
        "0.051"
    )
    assert settled[1].opening_fees_usdt.quantize(
        Decimal("0.0001")
    ) == Decimal("0.0025")
    assert await database.paired_perp_exposures(
        exchange="okx",
        environment="live",
    ) == [("ORDER-USDT-SWAP", Decimal("2"), 2)]
    assert repeated is not None
    assert repeated[2] is False
    await database.close()


async def test_live_settlement_closes_position_from_equal_base_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, position_id, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="close-spot-fill",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("20"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0.002"),
                fee_asset="USDT",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=legs["perp"],
        fills=[
            RemoteFill(
                exchange_trade_id="close-perp-fill",
                exchange_order_id="close-perp",
                client_order_id=None,
                market="perp",
                symbol="ORDER-USDT-SWAP",
                side="buy",
                quantity=Decimal("2"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0.002"),
                fee_asset="USDT",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_close(intent_id=intent_id)
    repeated = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "closed"
    assert settled[1].id == position_id
    assert settled[1].status == "closed"
    assert settled[1].quantity == Decimal("0")
    assert settled[1].remaining_opening_fees_usdt == Decimal("0")
    assert settled[1].closing_fees_usdt == Decimal("0.004")
    assert settled[1].realized_pnl_usdt.quantize(
        Decimal("0.000001")
    ) == Decimal("0.026000")
    assert repeated is not None and repeated[2] is False
    assert (
        await database.daily_realized_pnl(
            environment="live",
            exchanges={"okx"},
            since=now - timedelta(minutes=1),
        )
    ).quantize(Decimal("0.000001")) == Decimal("0.026000")
    assert await database.daily_realized_pnl(
        environment="paper",
        exchanges={"okx"},
        since=now - timedelta(minutes=1),
    ) == Decimal("0")
    assert await database.paired_perp_exposures(
        exchange="okx",
        environment="live",
    ) == []
    await database.close()


async def test_live_settlement_reopens_position_after_equal_partial_close() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, position_id, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="partial-spot-fill",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("10"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=legs["perp"],
        fills=[
            RemoteFill(
                exchange_trade_id="partial-perp-fill",
                exchange_order_id="close-perp",
                client_order_id=None,
                market="perp",
                symbol="ORDER-USDT-SWAP",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "closed"
    assert settled[1].id == position_id
    assert settled[1].status == "open"
    assert settled[1].closing_intent_id is None
    assert settled[1].quantity == Decimal("10")
    assert settled[1].remaining_opening_fees_usdt == Decimal("0.005")
    assert settled[1].realized_pnl_usdt.quantize(
        Decimal("0.000001")
    ) == Decimal("0.015000")
    await database.close()


async def test_live_close_settlement_pauses_on_imbalanced_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.set_execution_control(state="ready", reason="test")
    intent_id, position_id, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="imbalanced-spot-fill",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("20"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "compensating"
    assert settled[1].id == position_id
    assert settled[1].status == "closing"
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    assert compensation.side == "buy"
    assert compensation.quantity == Decimal("20")
    assert compensation.reduce_only is False
    control = await database.execution_control()
    assert control is not None and control.state == "paused"
    await database.close()


async def test_live_settlement_pauses_on_imbalanced_terminal_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.set_execution_control(state="ready", reason="test")
    intent_id, legs, now = await _live_intent(database, terminal_status="canceled")
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="spot-fill",
                exchange_order_id="remote-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="buy",
                quantity=Decimal("20"),
                price=Decimal("0.049"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=legs["perp"],
        fills=[
            RemoteFill(
                exchange_trade_id="perp-fill",
                exchange_order_id="remote-perp",
                client_order_id=None,
                market="perp",
                symbol="ORDER-USDT-SWAP",
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_open(intent_id=intent_id)

    assert settled is not None
    assert settled[0].status == "compensating"
    assert settled[1] is None
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    assert compensation.side == "sell"
    assert compensation.quantity == Decimal("10")
    assert compensation.reduce_only is False
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert "manual exposure review" in control.reason
    await database.close()


async def test_live_settlement_fails_when_both_terminal_legs_have_zero_fills() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, _, _ = await _live_intent(database, terminal_status="canceled")

    settled = await database.settle_live_open(intent_id=intent_id)

    assert settled is not None
    assert settled[0].status == "failed"
    assert settled[1] is None
    assert (await database.list_paired_positions()) == []
    await database.close()


async def test_live_open_compensation_settles_common_position_and_cost() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, legs, now = await _live_intent(
        database,
        terminal_status="canceled",
    )
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="open-spot-imbalanced",
                exchange_order_id="remote-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="buy",
                quantity=Decimal("20"),
                price=Decimal("0.049"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.persist_remote_fills(
        order_leg_id=legs["perp"],
        fills=[
            RemoteFill(
                exchange_trade_id="open-perp-common",
                exchange_order_id="remote-perp",
                client_order_id=None,
                market="perp",
                symbol="ORDER-USDT-SWAP",
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    first = await database.settle_live_open(intent_id=intent_id)
    assert first is not None and first[0].status == "compensating"
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    await database.persist_remote_fills(
        order_leg_id=compensation.id,
        fills=[
            RemoteFill(
                exchange_trade_id="open-spot-compensation",
                exchange_order_id="remote-compensation",
                client_order_id=compensation.client_order_id,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("10"),
                price=Decimal("0.048"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_open(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "hedged"
    assert settled[1].quantity == Decimal("10")
    assert settled[1].opening_fees_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("0.010")
    repeated = await database.settle_live_open(intent_id=intent_id)
    assert repeated is not None and repeated[2] is False
    await database.close()


async def test_live_close_compensation_realizes_round_trip_loss() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, position_id, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="close-spot-only",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("20"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    first = await database.settle_live_close(intent_id=intent_id)
    assert first is not None and first[0].status == "compensating"
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    await database.persist_remote_fills(
        order_leg_id=compensation.id,
        fills=[
            RemoteFill(
                exchange_trade_id="close-spot-compensation",
                exchange_order_id="remote-close-compensation",
                client_order_id=compensation.client_order_id,
                market="spot",
                symbol="ORDER-USDT",
                side="buy",
                quantity=Decimal("20"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "failed"
    assert settled[1].id == position_id
    assert settled[1].status == "open"
    assert settled[1].quantity == Decimal("20")
    assert settled[1].realized_pnl_usdt.quantize(
        Decimal("0.001")
    ) == Decimal("-0.020")
    await database.close()


async def test_unfilled_live_compensation_requires_manual_review() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, _, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="unfilled-comp-primary",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("20"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    await database.settle_live_close(intent_id=intent_id)
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    async with database.sessions() as session:
        row = await session.get(type(compensation), compensation.id)
        assert row is not None
        row.status = "canceled"
        await session.commit()

    settled = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None
    assert settled[0].status == "manual_review"
    assert settled[1] is not None
    assert settled[1].status == "closing"
    await database.close()


async def test_live_close_missing_primary_price_requires_manual_review() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    intent_id, _, legs, now = await _live_close_intent(database)
    await database.persist_remote_fills(
        order_leg_id=legs["spot"],
        fills=[
            RemoteFill(
                exchange_trade_id="missing-primary-price-spot",
                exchange_order_id="close-spot",
                client_order_id=None,
                market="spot",
                symbol="ORDER-USDT",
                side="sell",
                quantity=Decimal("20"),
                price=Decimal("0.05"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )
    async with database.sessions() as session:
        perp = await session.get(OrderLegRow, legs["perp"])
        assert perp is not None
        perp.status = "filled"
        perp.filled_quantity = Decimal("1")
        perp.average_price = None
        await session.commit()

    first = await database.settle_live_close(intent_id=intent_id)
    assert first is not None and first[0].status == "compensating"
    stored = await database.trade_intent(intent_id)
    assert stored is not None
    compensation = next(
        item for item in stored[1] if item.leg == "spot_compensation"
    )
    await database.persist_remote_fills(
        order_leg_id=compensation.id,
        fills=[
            RemoteFill(
                exchange_trade_id="missing-primary-price-compensation",
                exchange_order_id="remote-missing-primary-price",
                client_order_id=compensation.client_order_id,
                market="spot",
                symbol="ORDER-USDT",
                side="buy",
                quantity=Decimal("10"),
                price=Decimal("0.051"),
                fee_amount=Decimal("0"),
                fee_asset="",
                liquidity="taker",
                occurred_at=now,
            )
        ],
    )

    settled = await database.settle_live_close(intent_id=intent_id)

    assert settled is not None and settled[1] is not None
    assert settled[0].status == "manual_review"
    assert settled[1].status == "closing"
    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    await database.close()
