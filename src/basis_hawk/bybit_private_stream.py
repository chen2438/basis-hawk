from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class BybitPrivateStreamConnection:
    exchange = Exchange.BYBIT
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout_seconds: float = 10,
        clock_ms: Callable[[], int] | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._connector = connector
        self._socket: Any | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._pong_waiter: asyncio.Future[None] | None = None

    @property
    def url(self) -> str:
        if self.environment == ExchangeEnvironment.SANDBOX:
            return "wss://stream-testnet.bybit.com/v5/private"
        return "wss://stream.bybit.com/v5/private"

    async def connect(self) -> None:
        await self.close()
        while not self._events.empty():
            self._events.get_nowait()
        try:
            self._socket = await self._connector(
                self.url,
                ping_interval=None,
                close_timeout=5,
            )
            expires = self.clock_ms() + 1_000
            signature = hmac.new(
                self.secrets.api_secret.encode(),
                f"GET/realtime{expires}".encode(),
                hashlib.sha256,
            ).hexdigest()
            await self._socket.send(
                json.dumps(
                    {
                        "req_id": "basisHawkAuth",
                        "op": "auth",
                        "args": [
                            self.secrets.api_key,
                            expires,
                            signature,
                        ],
                    }
                )
            )
            authentication = await self._receive_json()
            if (
                authentication.get("op") != "auth"
                or authentication.get("success") is not True
            ):
                raise RuntimeError("Bybit private stream authentication failed")

            await self._socket.send(
                json.dumps(
                    {
                        "req_id": "basisHawkPrivate",
                        "op": "subscribe",
                        "args": [
                            "order",
                            "execution",
                            "position",
                            "wallet",
                        ],
                    }
                )
            )
            subscription = await self._receive_json()
            if (
                subscription.get("op") != "subscribe"
                or subscription.get("success") is not True
            ):
                raise RuntimeError("Bybit private stream subscription failed")
            self._reader_task = asyncio.create_task(self._read())
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("Bybit private event stream closed")
        return event

    async def probe(self) -> None:
        if self._socket is None or self._reader_task is None:
            raise RuntimeError("Bybit private event stream is not connected")
        if self._pong_waiter is not None and not self._pong_waiter.done():
            raise RuntimeError("Bybit private event stream ping is already pending")
        self._pong_waiter = asyncio.get_running_loop().create_future()
        await self._socket.send(
            json.dumps(
                {
                    "req_id": "basisHawkPing",
                    "op": "ping",
                }
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._pong_waiter),
                timeout=self.timeout_seconds,
            )
        finally:
            self._pong_waiter = None

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None
        if self._pong_waiter is not None and not self._pong_waiter.done():
            self._pong_waiter.cancel()
        self._pong_waiter = None
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:
                pass
        self._socket = None

    async def _read(self) -> None:
        try:
            while True:
                event = self._decode(await self._socket.recv())
                if event.get("op") == "pong":
                    if self._pong_waiter is not None and not self._pong_waiter.done():
                        self._pong_waiter.set_result(None)
                    continue
                if event.get("success") is False:
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _receive_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise RuntimeError("Bybit private event stream is not connected")
        return self._decode(
            await asyncio.wait_for(
                self._socket.recv(),
                timeout=self.timeout_seconds,
            )
        )

    @staticmethod
    def _decode(value: str | bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Bybit private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Bybit private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
