from __future__ import annotations

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.storage import Database


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
