from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class OkxPrivateStreamConnection:
    exchange = Exchange.OKX
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout_seconds: float = 10,
        clock_seconds: Callable[[], float] | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        if not secrets.passphrase:
            raise ValueError("OKX private stream requires a passphrase")
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_seconds = clock_seconds or time.time
        self._connector = connector
        self._socket: Any | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._pong_waiter: asyncio.Future[None] | None = None

    @property
    def url(self) -> str:
        if self.environment == ExchangeEnvironment.SANDBOX:
            return "wss://wspap.okx.com:8443/ws/v5/private"
        return "wss://ws.okx.com:8443/ws/v5/private"

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
            timestamp = str(int(self.clock_seconds()))
            signature = base64.b64encode(
                hmac.new(
                    self.secrets.api_secret.encode(),
                    f"{timestamp}GET/users/self/verify".encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            await self._socket.send(
                json.dumps(
                    {
                        "op": "login",
                        "args": [
                            {
                                "apiKey": self.secrets.api_key,
                                "passphrase": self.secrets.passphrase,
                                "timestamp": timestamp,
                                "sign": signature,
                            }
                        ],
                    }
                )
            )
            login = await self._receive_json()
            if login.get("event") != "login" or login.get("code") != "0":
                raise RuntimeError("OKX private stream authentication failed")

            subscriptions = [
                {"channel": "orders", "instType": "ANY"},
                {"channel": "positions", "instType": "ANY"},
                {"channel": "account"},
            ]
            await self._socket.send(
                json.dumps(
                    {
                        "id": "basisHawkPrivate",
                        "op": "subscribe",
                        "args": subscriptions,
                    }
                )
            )
            acknowledged: set[str] = set()
            while acknowledged != {"orders", "positions", "account"}:
                response = await self._receive_json()
                if response.get("event") == "error":
                    raise RuntimeError("OKX private stream subscription failed")
                if response.get("event") != "subscribe":
                    continue
                argument = response.get("arg")
                if not isinstance(argument, dict):
                    raise RuntimeError("OKX private stream subscription failed")
                channel = str(argument.get("channel") or "")
                if channel in {"orders", "positions", "account"}:
                    acknowledged.add(channel)
            self._reader_task = asyncio.create_task(self._read())
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("OKX private event stream closed")
        return event

    async def probe(self) -> None:
        if self._socket is None or self._reader_task is None:
            raise RuntimeError("OKX private event stream is not connected")
        if self._pong_waiter is not None and not self._pong_waiter.done():
            raise RuntimeError("OKX private event stream ping is already pending")
        self._pong_waiter = asyncio.get_running_loop().create_future()
        await self._socket.send("ping")
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
                raw = await self._socket.recv()
                if raw == "pong" or raw == b"pong":
                    if self._pong_waiter is not None and not self._pong_waiter.done():
                        self._pong_waiter.set_result(None)
                    continue
                event = self._decode(raw)
                if event.get("event") in {
                    "error",
                    "channel-conn-count-error",
                }:
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _receive_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise RuntimeError("OKX private event stream is not connected")
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
            raise RuntimeError("OKX private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("OKX private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
