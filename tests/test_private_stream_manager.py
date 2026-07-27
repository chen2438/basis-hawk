import asyncio
from pathlib import Path

from basis_hawk.credentials import (
    CredentialService,
    CredentialSummary,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.private_stream import (
    DynamicPrivateStreamManager,
    PrivateStreamRegistry,
    PrivateStreamSupervisor,
)
from basis_hawk.storage import Database


class FakeConnection:
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    def __init__(self, summary: CredentialSummary) -> None:
        self.exchange = summary.exchange
        self.environment = summary.environment
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        self.connected.set()

    async def receive(self) -> object:
        await asyncio.Future()

    async def probe(self) -> None:
        return None

    async def close(self) -> None:
        self.closed.set()


async def _wait_for(predicate) -> None:
    for _ in range(200):
        if await predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def test_manager_hot_adds_replaces_and_removes_private_streams(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'streams.db'}")
    await database.initialize()
    await database.set_execution_control(state="ready", reason="test ready")
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    connections: list[FakeConnection] = []

    async def connection_factory(summary: CredentialSummary) -> FakeConnection:
        connection = FakeConnection(summary)
        connections.append(connection)
        return connection

    supervisor = PrivateStreamSupervisor(
        PrivateStreamRegistry(database),
        receive_timeout_seconds=60,
    )
    manager = DynamicPrivateStreamManager(
        supervisor,
        credentials.list,
        connection_factory,
        refresh_seconds=0.01,
    )
    task = asyncio.create_task(manager.run())
    await credentials.save(
        exchange=Exchange.GATE,
        environment=ExchangeEnvironment.LIVE,
        label="primary",
        secrets=ExchangeSecrets(
            api_key="gate-api-key",
            api_secret="gate-api-secret",
        ),
        actor="test",
    )

    await _wait_for(
        lambda: database.private_stream_ready(
            exchange="gate",
            environment="live",
        )
    )
    assert len(connections) == 1
    await asyncio.sleep(0.03)
    assert len(connections) == 1

    await credentials.save(
        exchange=Exchange.GATE,
        environment=ExchangeEnvironment.LIVE,
        label="replacement",
        secrets=ExchangeSecrets(
            api_key="new-gate-api-key",
            api_secret="new-gate-api-secret",
        ),
        actor="test",
    )

    await _wait_for(lambda: _event_is_set(connections[0].closed))
    await _wait_for(lambda: _connection_count_is(connections, 2))
    await _wait_for(lambda: _event_is_set(connections[1].connected))
    await _wait_for(
        lambda: database.private_stream_ready(
            exchange="gate",
            environment="live",
        )
    )

    assert (
        await credentials.delete(
            Exchange.GATE,
            ExchangeEnvironment.LIVE,
            actor="test",
        )
        is True
    )
    await _wait_for(lambda: _event_is_set(connections[1].closed))
    await _wait_for(
        lambda: _private_stream_is_disconnected(database, "gate", "live")
    )

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await database.close()


async def _event_is_set(event: asyncio.Event) -> bool:
    return event.is_set()


async def _connection_count_is(
    connections: list[FakeConnection],
    expected: int,
) -> bool:
    return len(connections) == expected


async def _private_stream_is_disconnected(
    database: Database,
    exchange: str,
    environment: str,
) -> bool:
    state = await database.private_stream_state(
        exchange=exchange,
        environment=environment,
    )
    return state is not None and not state.connected
