import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.notifications import NotificationProjectionService
from basis_hawk.storage import Database, TradeIntentRow


async def test_projection_bootstraps_without_replaying_existing_alerts() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.set_execution_control(
        state="paused",
        reason="existing safety pause",
    )
    projector = NotificationProjectionService(database)

    assert await projector.run_once(emit_initial_alerts=False) == 0
    assert await database.notification_outbox() == []
    assert await projector.run_once() == 0
    assert await database.notification_outbox() == []
    await database.close()


async def test_projection_emits_pause_once_and_again_after_recovery() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    projector = NotificationProjectionService(database)
    await database.set_execution_control(state="ready", reason="healthy")
    await projector.run_once(emit_initial_alerts=False)

    await database.set_execution_control(state="paused", reason="stream down")
    assert await projector.run_once() == 1
    assert await projector.run_once() == 0
    first = await database.notification_outbox()
    assert len(first) == 2
    assert {item.channel for item in first} == {"telegram", "email"}
    assert {item.event_type for item in first} == {"execution.paused"}

    await database.set_execution_control(state="ready", reason="reconciled")
    assert await projector.run_once() == 0
    await database.set_execution_control(state="paused", reason="stream down")
    assert await projector.run_once() == 1
    rows = await database.notification_outbox()
    assert len(rows) == 4
    assert len({item.dedupe_key for item in rows}) == 2
    await database.close()


async def test_projection_emits_account_transition_without_reason_text() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    projector = NotificationProjectionService(database)
    await database.record_account_reconciliation(
        exchange="bybit",
        environment="live",
        status="ready",
        reason="account reconciliation passed",
    )
    await projector.run_once(emit_initial_alerts=False)

    await database.record_account_reconciliation(
        exchange="bybit",
        environment="live",
        status="error",
        reason="private account reconciliation failed token=not-for-message",
    )
    assert await projector.run_once() == 1
    rows = await database.notification_outbox()
    assert len(rows) == 2
    assert {item.event_type for item in rows} == {"account.error"}
    assert all("token=" not in item.body for item in rows)
    await database.close()


async def test_projection_routes_trade_success_and_imbalance() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime.now(UTC)
    intent_id = str(uuid.uuid4())
    async with database.sessions() as session:
        session.add(
            TradeIntentRow(
                id=intent_id,
                paired_position_id=None,
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint="f" * 64,
                exchange="bybit",
                environment="live",
                base_asset="ORDER",
                action="open",
                emergency=False,
                status="executing",
                leverage=1,
                requested_notional=Decimal("10"),
                base_quantity=Decimal("100"),
                spot_fee_rate=Decimal("0.001"),
                perp_fee_rate=Decimal("0.00055"),
                market_observed_at=now,
                config_version="config",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    projector = NotificationProjectionService(database)
    await projector.run_once(emit_initial_alerts=False)

    await database.transition_trade_intent(
        intent_id=intent_id,
        expected_version=1,
        status="compensating",
    )
    assert await projector.run_once() == 1
    imbalance = await database.notification_outbox()
    assert len(imbalance) == 2
    assert {item.event_type for item in imbalance} == {"trade.imbalance"}

    await database.transition_trade_intent(
        intent_id=intent_id,
        expected_version=2,
        status="hedged",
    )
    assert await projector.run_once() == 1
    rows = await database.notification_outbox()
    hedged = [item for item in rows if item.event_type == "trade.hedged"]
    assert len(hedged) == 1
    assert hedged[0].channel == "telegram"
    await database.close()


async def test_projection_sends_one_summary_for_completed_utc_day() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    projector = NotificationProjectionService(database)
    first_day = datetime(2026, 7, 26, 12, tzinfo=UTC)

    assert (
        await projector.run_once(
            emit_initial_alerts=False,
            now=first_day,
        )
        == 0
    )
    assert await projector.run_once(now=first_day + timedelta(hours=6)) == 0
    assert await projector.run_once(now=first_day + timedelta(days=1)) == 1
    rows = await database.notification_outbox()
    assert len(rows) == 2
    assert {item.channel for item in rows} == {"telegram", "email"}
    assert {item.event_type for item in rows} == {"system.daily_summary"}
    assert all("2026-07-26" in item.subject for item in rows)
    assert all("Realized PnL: 0 USDT" in item.body for item in rows)
    assert await projector.run_once(now=first_day + timedelta(days=1, hours=1)) == 0
    await database.close()
