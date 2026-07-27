from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from basis_hawk.credentials import CredentialSummary, ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.storage import Database

logger = logging.getLogger(__name__)


class PrivateStreamConnection(Protocol):
    exchange: Exchange
    environment: ExchangeEnvironment
    orders_subscribed: bool
    fills_subscribed: bool
    positions_subscribed: bool

    async def connect(self) -> None: ...

    async def receive(self) -> object: ...

    async def probe(self) -> None: ...

    async def close(self) -> None: ...


EventHandler = Callable[[PrivateStreamConnection, object], Awaitable[None]]
StateHandler = Callable[[PrivateStreamConnection, bool], None]
SummaryProvider = Callable[[], Awaitable[Sequence[CredentialSummary]]]
ConnectionFactory = Callable[
    [CredentialSummary],
    Awaitable[PrivateStreamConnection],
]


class PrivateStreamRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def startup_reset(self) -> None:
        await self.database.reset_private_stream_states()

    async def connected(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        orders_subscribed: bool,
        fills_subscribed: bool,
        positions_subscribed: bool,
    ) -> None:
        await self.database.set_private_stream_state(
            exchange=exchange.value,
            environment=environment.value,
            connected=True,
            authenticated=True,
            orders_subscribed=orders_subscribed,
            fills_subscribed=fills_subscribed,
            positions_subscribed=positions_subscribed,
            heartbeat=True,
        )

    async def heartbeat(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        event: bool = False,
    ) -> None:
        current = await self.database.private_stream_state(
            exchange=exchange.value,
            environment=environment.value,
        )
        if current is None or not current.connected:
            raise RuntimeError("private stream is not connected")
        await self.database.set_private_stream_state(
            exchange=exchange.value,
            environment=environment.value,
            connected=True,
            authenticated=current.authenticated,
            orders_subscribed=current.orders_subscribed,
            fills_subscribed=current.fills_subscribed,
            positions_subscribed=current.positions_subscribed,
            heartbeat=True,
            event=event,
        )

    async def disconnected(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
    ) -> None:
        await self.database.set_private_stream_state(
            exchange=exchange.value,
            environment=environment.value,
            connected=False,
            authenticated=False,
            orders_subscribed=False,
            fills_subscribed=False,
            positions_subscribed=False,
        )


class PrivateStreamSupervisor:
    def __init__(
        self,
        registry: PrivateStreamRegistry,
        *,
        receive_timeout_seconds: float = 10,
        reconnect_initial_seconds: float = 1,
        reconnect_max_seconds: float = 30,
        event_handler: EventHandler | None = None,
        state_handler: StateHandler | None = None,
    ) -> None:
        if receive_timeout_seconds <= 0:
            raise ValueError("private stream receive timeout must be positive")
        if reconnect_initial_seconds <= 0:
            raise ValueError("private stream reconnect delay must be positive")
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("maximum reconnect delay cannot be below initial delay")
        self.registry = registry
        self.receive_timeout_seconds = receive_timeout_seconds
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.event_handler = event_handler
        self.state_handler = state_handler

    async def run(self, connections: Sequence[PrivateStreamConnection]) -> None:
        if not connections:
            await asyncio.Future()
        async with asyncio.TaskGroup() as tasks:
            for connection in connections:
                tasks.create_task(self.supervise(connection))

    async def run_connection_once(
        self,
        connection: PrivateStreamConnection,
    ) -> None:
        registered = False
        try:
            await connection.connect()
            await self.registry.connected(
                exchange=connection.exchange,
                environment=connection.environment,
                orders_subscribed=connection.orders_subscribed,
                fills_subscribed=connection.fills_subscribed,
                positions_subscribed=connection.positions_subscribed,
            )
            registered = True
            if self.state_handler is not None:
                self.state_handler(connection, True)
            while True:
                try:
                    event = await asyncio.wait_for(
                        connection.receive(),
                        timeout=self.receive_timeout_seconds,
                    )
                except TimeoutError:
                    await connection.probe()
                    await self.registry.heartbeat(
                        exchange=connection.exchange,
                        environment=connection.environment,
                    )
                    continue
                if self.event_handler is not None:
                    await self.event_handler(connection, event)
                await self.registry.heartbeat(
                    exchange=connection.exchange,
                    environment=connection.environment,
                    event=True,
                )
        finally:
            try:
                await connection.close()
            finally:
                if registered:
                    await self.registry.disconnected(
                        exchange=connection.exchange,
                        environment=connection.environment,
                    )
                    if self.state_handler is not None:
                        self.state_handler(connection, False)

    async def supervise(self, connection: PrivateStreamConnection) -> None:
        delay = self.reconnect_initial_seconds
        while True:
            try:
                await self.run_connection_once(connection)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "private event stream disconnected; scheduling reconnect",
                    extra={
                        "exchange": connection.exchange.value,
                        "environment": connection.environment.value,
                    },
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max_seconds)


@dataclass
class _ManagedPrivateStream:
    revision: datetime
    connection: PrivateStreamConnection
    task: asyncio.Task[None]


class DynamicPrivateStreamManager:
    def __init__(
        self,
        supervisor: PrivateStreamSupervisor,
        summary_provider: SummaryProvider,
        connection_factory: ConnectionFactory,
        *,
        refresh_seconds: float = 1,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("private stream refresh interval must be positive")
        self.supervisor = supervisor
        self.summary_provider = summary_provider
        self.connection_factory = connection_factory
        self.refresh_seconds = refresh_seconds

    async def run(self) -> None:
        managed: dict[
            tuple[Exchange, ExchangeEnvironment],
            _ManagedPrivateStream,
        ] = {}
        try:
            while True:
                summaries = {
                    (item.exchange, item.environment): item
                    for item in await self.summary_provider()
                }
                desired_revisions = {
                    key: summary.updated_at for key, summary in summaries.items()
                }
                current_revisions = {
                    key: value.revision for key, value in managed.items()
                }
                if desired_revisions != current_revisions:
                    await self.supervisor.registry.database.request_execution_reconciliation(
                        reason="exchange credential configuration changed",
                    )

                for key in sorted(
                    set(managed) - set(summaries)
                    | {
                        key
                        for key in set(managed) & set(summaries)
                        if managed[key].revision != summaries[key].updated_at
                    },
                    key=lambda item: (item[0].value, item[1].value),
                ):
                    await self._stop(managed.pop(key))

                for key, summary in summaries.items():
                    if key in managed:
                        continue
                    try:
                        connection = await self.connection_factory(summary)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "private event stream configuration failed; "
                            "scheduling retry",
                            extra={
                                "exchange": summary.exchange.value,
                                "environment": summary.environment.value,
                            },
                        )
                        await self.supervisor.registry.disconnected(
                            exchange=summary.exchange,
                            environment=summary.environment,
                        )
                        continue
                    managed[key] = _ManagedPrivateStream(
                        revision=summary.updated_at,
                        connection=connection,
                        task=asyncio.create_task(
                            self.supervisor.supervise(connection)
                        ),
                    )
                await asyncio.sleep(self.refresh_seconds)
        finally:
            for value in list(managed.values()):
                await self._stop(value)

    async def _stop(self, value: _ManagedPrivateStream) -> None:
        value.task.cancel()
        await asyncio.gather(value.task, return_exceptions=True)
        await self.supervisor.registry.disconnected(
            exchange=value.connection.exchange,
            environment=value.connection.environment,
        )
