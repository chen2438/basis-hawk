from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.accounts import GateAccountClient
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class GatePrivateStreamConnection:
    exchange = Exchange.GATE
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout_seconds: float = 10,
        clock_s: Callable[[], int] | None = None,
        user_id_resolver: Callable[[], Awaitable[str]] | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_s = clock_s or (lambda: int(time.time()))
        self._user_id_resolver = user_id_resolver or self._resolve_user_id
        self._connector = connector
        self._spot_socket: Any | None = None
        self._futures_socket: Any | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_tasks: list[asyncio.Task[None]] = []

    async def connect(self) -> None:
        await self.close()
        while not self._events.empty():
            self._events.get_nowait()
        if self.environment != ExchangeEnvironment.LIVE:
            raise RuntimeError(
                "Gate private stream requires the live environment"
            )
        try:
            user_id = await self._user_id_resolver()
            if not user_id.isdigit() or int(user_id) <= 0:
                raise RuntimeError(
                    "Gate futures account user identifier is invalid"
                )
            self._spot_socket = await self._connector(
                "wss://api.gateio.ws/ws/v4/",
                ping_interval=None,
                close_timeout=5,
            )
            self._futures_socket = await self._connector(
                "wss://fx-ws.gateio.ws/v4/ws/usdt",
                ping_interval=None,
                close_timeout=5,
                additional_headers={"X-Gate-Size-Decimal": "1"},
            )
            for channel in ("spot.orders", "spot.usertrades"):
                await self._subscribe(
                    self._spot_socket,
                    channel=channel,
                    payload=["!all"],
                )
            for channel in (
                "futures.orders",
                "futures.usertrades",
                "futures.positions",
            ):
                await self._subscribe(
                    self._futures_socket,
                    channel=channel,
                    payload=[user_id, "!all"],
                )
            self._reader_tasks = [
                asyncio.create_task(self._read(self._spot_socket)),
                asyncio.create_task(self._read(self._futures_socket)),
            ]
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("Gate private event stream closed")
        return event

    async def probe(self) -> None:
        if (
            self._spot_socket is None
            or self._futures_socket is None
            or len(self._reader_tasks) != 2
        ):
            raise RuntimeError("Gate private event stream is not connected")
        pongs = [
            await self._spot_socket.ping(),
            await self._futures_socket.ping(),
        ]
        await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(pong) for pong in pongs)),
            timeout=self.timeout_seconds,
        )

    async def close(self) -> None:
        for task in self._reader_tasks:
            task.cancel()
        if self._reader_tasks:
            await asyncio.gather(*self._reader_tasks, return_exceptions=True)
        self._reader_tasks = []
        for socket in (self._spot_socket, self._futures_socket):
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    pass
        self._spot_socket = None
        self._futures_socket = None

    async def _subscribe(
        self,
        socket: Any,
        *,
        channel: str,
        payload: list[str],
    ) -> None:
        timestamp = self.clock_s()
        signature = hmac.new(
            self.secrets.api_secret.encode(),
            (
                f"channel={channel}&event=subscribe&time={timestamp}"
            ).encode(),
            hashlib.sha512,
        ).hexdigest()
        await socket.send(
            json.dumps(
                {
                    "time": timestamp,
                    "channel": channel,
                    "event": "subscribe",
                    "payload": payload,
                    "auth": {
                        "method": "api_key",
                        "KEY": self.secrets.api_key,
                        "SIGN": signature,
                    },
                }
            )
        )
        while True:
            response = self._decode(
                await asyncio.wait_for(
                    socket.recv(),
                    timeout=self.timeout_seconds,
                )
            )
            if (
                response.get("channel") == channel
                and response.get("event") == "subscribe"
            ):
                result = response.get("result")
                if (
                    response.get("error") is not None
                    or not isinstance(result, dict)
                    or result.get("status") != "success"
                ):
                    raise RuntimeError(
                        "Gate private stream subscription failed"
                    )
                return
            await self._events.put(response)

    async def _read(self, socket: Any) -> None:
        try:
            while True:
                event = self._decode(await socket.recv())
                if event.get("error") is not None:
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _resolve_user_id(self) -> str:
        client = GateAccountClient(
            self.secrets,
            self.environment,
            timeout=self.timeout_seconds,
            clock_s=self.clock_s,
        )
        try:
            return await client.user_id()
        finally:
            await client.close()

    @staticmethod
    def _decode(value: str | bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Gate private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Gate private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
