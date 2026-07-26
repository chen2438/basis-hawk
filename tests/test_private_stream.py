from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.private_stream import PrivateStreamRegistry
from basis_hawk.storage import Database, PrivateStreamStateRow


async def test_private_stream_requires_all_subscriptions_and_fresh_heartbeat() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    registry = PrivateStreamRegistry(database)

    await registry.connected(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        orders_subscribed=True,
        fills_subscribed=False,
        positions_subscribed=True,
    )
    assert (
        await database.private_stream_ready(
            exchange="binance",
            environment="live",
        )
        is False
    )

    await registry.connected(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        orders_subscribed=True,
        fills_subscribed=True,
        positions_subscribed=True,
    )
    assert (
        await database.private_stream_ready(
            exchange="binance",
            environment="live",
        )
        is True
    )

    async with database.sessions() as session:
        await session.execute(
            update(PrivateStreamStateRow)
            .where(
                PrivateStreamStateRow.exchange == "binance",
                PrivateStreamStateRow.environment == "live",
            )
            .values(last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=31))
        )
        await session.commit()
    assert (
        await database.private_stream_ready(
            exchange="binance",
            environment="live",
        )
        is False
    )
    await registry.heartbeat(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        event=True,
    )
    state = await database.private_stream_state(
        exchange="binance",
        environment="live",
    )
    assert state is not None
    assert state.last_event_at is not None
    assert (
        await database.private_stream_ready(
            exchange="binance",
            environment="live",
        )
        is True
    )
    await database.close()


async def test_disconnect_pauses_ready_execution_and_startup_reset_is_fail_closed() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    registry = PrivateStreamRegistry(database)
    await registry.connected(
        exchange=Exchange.OKX,
        environment=ExchangeEnvironment.SANDBOX,
        orders_subscribed=True,
        fills_subscribed=True,
        positions_subscribed=True,
    )
    await database.set_execution_control(
        state="ready",
        reason="startup reconciliation passed",
    )

    await registry.disconnected(
        exchange=Exchange.OKX,
        environment=ExchangeEnvironment.SANDBOX,
    )

    control = await database.execution_control()
    assert control is not None
    assert control.state == "paused"
    assert control.reason == (
        "private account event stream disconnected; REST reconciliation is required"
    )
    assert (
        await database.private_stream_ready(
            exchange="okx",
            environment="sandbox",
        )
        is False
    )

    await registry.connected(
        exchange=Exchange.OKX,
        environment=ExchangeEnvironment.SANDBOX,
        orders_subscribed=True,
        fills_subscribed=True,
        positions_subscribed=True,
    )
    await registry.startup_reset()
    state = await database.private_stream_state(
        exchange="okx",
        environment="sandbox",
    )
    assert state is not None
    assert state.connected is False
    assert state.authenticated is False
    assert state.orders_subscribed is False
    await database.close()
