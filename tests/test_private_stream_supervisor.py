import asyncio
from pathlib import Path

import pytest

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.private_stream import (
    PrivateStreamRegistry,
    PrivateStreamSupervisor,
)
from basis_hawk.storage import Database


class FakeConnection:
    exchange = Exchange.BYBIT
    environment = ExchangeEnvironment.LIVE
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.connected = False
        self.closed = False
        self.probes = 0

    async def connect(self) -> None:
        self.connected = True

    async def receive(self) -> object:
        if self.events:
            value = self.events.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        await asyncio.Future()

    async def probe(self) -> None:
        self.probes += 1

    async def close(self) -> None:
        self.closed = True


async def test_supervisor_tracks_events_and_disconnects_fail_closed() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    registry = PrivateStreamRegistry(database)
    seen: list[object] = []
    states: list[bool] = []

    async def on_event(connection: FakeConnection, event: object) -> None:
        assert connection.exchange == Exchange.BYBIT
        seen.append(event)

    supervisor = PrivateStreamSupervisor(
        registry,
        receive_timeout_seconds=0.01,
        event_handler=on_event,
        state_handler=lambda _connection, connected: states.append(connected),
    )
    connection = FakeConnection([{"topic": "order"}, RuntimeError("closed")])

    with pytest.raises(RuntimeError, match="closed"):
        await supervisor.run_connection_once(connection)

    assert seen == [{"topic": "order"}]
    assert states == [True, False]
    assert connection.connected is True
    assert connection.closed is True
    state = await database.private_stream_state(
        exchange="bybit",
        environment="live",
    )
    assert state is not None
    assert state.connected is False
    assert state.last_event_at is not None
    await database.close()


async def test_supervisor_uses_verified_probe_for_idle_heartbeat(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stream.db'}")
    await database.initialize()
    supervisor = PrivateStreamSupervisor(
        PrivateStreamRegistry(database),
        receive_timeout_seconds=0.01,
    )
    connection = FakeConnection([])
    task = asyncio.create_task(supervisor.run_connection_once(connection))

    for _ in range(50):
        if connection.probes:
            break
        await asyncio.sleep(0.002)
    assert connection.probes >= 1
    assert (
        await database.private_stream_ready(
            exchange="bybit",
            environment="live",
        )
        is True
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await database.private_stream_state(
        exchange="bybit",
        environment="live",
    )
    assert state is not None
    assert state.connected is False
    await database.close()
