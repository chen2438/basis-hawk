import uuid
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.accounts import RemoteFill
from basis_hawk.storage import Database


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
    assert repeated is not None
    assert repeated[2] is False
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
    assert settled[0].status == "manual_review"
    assert settled[1] is None
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
