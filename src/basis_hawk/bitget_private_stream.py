from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.accounts import BitgetAccountClient
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange

AccountGeneration = Literal["classic", "uta"]


class BitgetPrivateStreamConnection:
    exchange = Exchange.BITGET
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
        generation_resolver: Callable[[], Awaitable[AccountGeneration]] | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        if not secrets.passphrase:
            raise ValueError("Bitget private stream requires a passphrase")
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._generation_resolver = generation_resolver or self._resolve_generation
        self._connector = connector
        self._generation: AccountGeneration | None = None
        self._socket: Any | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._pong_waiter: asyncio.Future[None] | None = None

    @property
    def url(self) -> str:
        if self._generation is None:
            raise RuntimeError("Bitget account generation has not been resolved")
        host = (
            "wspap.bitget.com"
            if self.environment == ExchangeEnvironment.SANDBOX
            else "ws.bitget.com"
        )
        version = "v3" if self._generation == "uta" else "v2"
        return f"wss://{host}/{version}/ws/private"

    async def connect(self) -> None:
        await self.close()
        while not self._events.empty():
            self._events.get_nowait()
        try:
            self._generation = await self._generation_resolver()
            if self._generation not in {"classic", "uta"}:
                raise RuntimeError("Bitget account generation could not be identified")
            self._socket = await self._connector(
                self.url,
                ping_interval=None,
                close_timeout=5,
            )
            timestamp = str(self.clock_ms() // 1_000)
            signature = base64.b64encode(
                hmac.new(
                    self.secrets.api_secret.encode(),
                    f"{timestamp}GET/user/verify".encode(),
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
            authentication = await self._receive_json()
            if (
                authentication.get("event") != "login"
                or authentication.get("code") != "0"
            ):
                raise RuntimeError("Bitget private stream authentication failed")

            subscriptions = self._subscriptions()
            await self._socket.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "args": subscriptions,
                    }
                )
            )
            expected = {self._subscription_key(item) for item in subscriptions}
            acknowledged: set[tuple[str, str]] = set()
            while acknowledged != expected:
                response = await self._receive_json()
                if response.get("event") == "error":
                    raise RuntimeError("Bitget private stream subscription failed")
                if response.get("event") == "subscribe":
                    argument = response.get("arg")
                    if not isinstance(argument, dict):
                        raise RuntimeError("Bitget private stream subscription failed")
                    key = self._subscription_key(argument)
                    if key in expected:
                        acknowledged.add(key)
                    continue
                await self._events.put(response)
            self._reader_task = asyncio.create_task(self._read())
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("Bitget private event stream closed")
        return event

    async def probe(self) -> None:
        if self._socket is None or self._reader_task is None:
            raise RuntimeError("Bitget private event stream is not connected")
        if self._pong_waiter is not None and not self._pong_waiter.done():
            raise RuntimeError("Bitget private event stream ping is already pending")
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
                if event.get("event") == "error":
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _receive_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise RuntimeError("Bitget private event stream is not connected")
        return self._decode(
            await asyncio.wait_for(
                self._socket.recv(),
                timeout=self.timeout_seconds,
            )
        )

    async def _resolve_generation(self) -> AccountGeneration:
        client = BitgetAccountClient(
            self.secrets,
            self.environment,
            timeout=self.timeout_seconds,
            clock_ms=self.clock_ms,
        )
        try:
            return await client.account_generation()
        finally:
            await client.close()

    def _subscriptions(self) -> list[dict[str, str]]:
        if self._generation == "uta":
            return [
                {"instType": "UTA", "topic": topic}
                for topic in ("order", "fill", "position", "account")
            ]
        return [
            {"instType": "SPOT", "channel": "orders", "instId": "default"},
            {"instType": "SPOT", "channel": "fill", "instId": "default"},
            {
                "instType": "USDT-FUTURES",
                "channel": "orders",
                "instId": "default",
            },
            {
                "instType": "USDT-FUTURES",
                "channel": "fill",
                "instId": "default",
            },
            {
                "instType": "USDT-FUTURES",
                "channel": "positions",
                "instId": "default",
            },
            {"instType": "SPOT", "channel": "account", "coin": "default"},
            {
                "instType": "USDT-FUTURES",
                "channel": "account",
                "coin": "default",
            },
        ]

    @staticmethod
    def _subscription_key(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("instType") or ""),
            str(item.get("topic") or item.get("channel") or ""),
        )

    @staticmethod
    def _decode(value: str | bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Bitget private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Bitget private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
