from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class MexcPrivateStreamConnection:
    exchange = Exchange.MEXC
    orders_subscribed = True
    fills_subscribed = True
    positions_subscribed = True

    _SPOT_CHANNELS = (
        "spot@private.orders.v3.api.pb",
        "spot@private.deals.v3.api.pb",
        "spot@private.account.v3.api.pb",
    )

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout_seconds: float = 10,
        clock_ms: Callable[[], int] | None = None,
        spot_client: httpx.AsyncClient | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._spot_client = spot_client
        self._owned_spot_client = spot_client is None
        self._connector = connector
        self._spot_socket: Any | None = None
        self._futures_socket: Any | None = None
        self._listen_key: str | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._keepalive_task: asyncio.Task[None] | None = None
        self._spot_pong: asyncio.Future[None] | None = None
        self._futures_pong: asyncio.Future[None] | None = None

    async def connect(self) -> None:
        await self.close()
        while not self._events.empty():
            self._events.get_nowait()
        if self.environment != ExchangeEnvironment.LIVE:
            raise RuntimeError(
                "MEXC private stream requires the live environment"
            )
        try:
            response = await self._client().post(
                "/api/v3/userDataStream",
                headers={"X-MEXC-APIKEY": self.secrets.api_key},
            )
            if not response.is_success:
                raise RuntimeError(
                    "MEXC spot private stream authentication failed"
                )
            try:
                listen_key = str(response.json()["listenKey"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "MEXC spot private stream authentication failed"
                ) from exc
            if not listen_key:
                raise RuntimeError(
                    "MEXC spot private stream authentication failed"
                )
            self._listen_key = listen_key
            query = urlencode({"listenKey": listen_key})
            self._spot_socket = await self._connector(
                f"wss://wbs-api.mexc.com/ws?{query}",
                ping_interval=None,
                close_timeout=5,
            )
            for channel in self._SPOT_CHANNELS:
                await self._subscribe_spot(channel)

            self._futures_socket = await self._connector(
                "wss://contract.mexc.com/edge",
                ping_interval=None,
                close_timeout=5,
            )
            timestamp = str(self.clock_ms())
            signature = hmac.new(
                self.secrets.api_secret.encode(),
                f"{self.secrets.api_key}{timestamp}".encode(),
                hashlib.sha256,
            ).hexdigest()
            await self._futures_socket.send(
                json.dumps(
                    {
                        "method": "login",
                        "param": {
                            "apiKey": self.secrets.api_key,
                            "reqTime": timestamp,
                            "signature": signature,
                        },
                    }
                )
            )
            login = await self._receive_json(self._futures_socket)
            if (
                login.get("channel") != "rs.login"
                or login.get("data") != "success"
            ):
                raise RuntimeError(
                    "MEXC futures private stream authentication failed"
                )

            self._reader_tasks = [
                asyncio.create_task(self._read_spot()),
                asyncio.create_task(self._read_futures()),
            ]
            self._keepalive_task = asyncio.create_task(self._keepalive())
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("MEXC private event stream closed")
        return event

    async def probe(self) -> None:
        if (
            self._spot_socket is None
            or self._futures_socket is None
            or len(self._reader_tasks) != 2
        ):
            raise RuntimeError("MEXC private event stream is not connected")
        if (
            self._spot_pong is not None
            and not self._spot_pong.done()
            or self._futures_pong is not None
            and not self._futures_pong.done()
        ):
            raise RuntimeError("MEXC private event stream ping is already pending")
        loop = asyncio.get_running_loop()
        self._spot_pong = loop.create_future()
        self._futures_pong = loop.create_future()
        await self._spot_socket.send(json.dumps({"method": "PING"}))
        await self._futures_socket.send(json.dumps({"method": "ping"}))
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    asyncio.shield(self._spot_pong),
                    asyncio.shield(self._futures_pong),
                ),
                timeout=self.timeout_seconds,
            )
        finally:
            self._spot_pong = None
            self._futures_pong = None

    async def close(self) -> None:
        tasks = [*self._reader_tasks]
        if self._keepalive_task is not None:
            tasks.append(self._keepalive_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_tasks = []
        self._keepalive_task = None
        for waiter in (self._spot_pong, self._futures_pong):
            if waiter is not None and not waiter.done():
                waiter.cancel()
        self._spot_pong = None
        self._futures_pong = None
        for socket in (self._spot_socket, self._futures_socket):
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    pass
        self._spot_socket = None
        self._futures_socket = None
        if self._listen_key and self._spot_client is not None:
            try:
                await self._spot_client.delete(
                    "/api/v3/userDataStream",
                    params={"listenKey": self._listen_key},
                    headers={"X-MEXC-APIKEY": self.secrets.api_key},
                )
            except Exception:
                pass
        self._listen_key = None
        if (
            self._owned_spot_client
            and self._spot_client is not None
            and not self._spot_client.is_closed
        ):
            await self._spot_client.aclose()

    async def _subscribe_spot(self, channel: str) -> None:
        if self._spot_socket is None:
            raise RuntimeError("MEXC spot private event stream is not connected")
        await self._spot_socket.send(
            json.dumps(
                {
                    "method": "SUBSCRIPTION",
                    "params": [channel],
                }
            )
        )
        response = await self._receive_json(self._spot_socket)
        if response.get("code") != 0 or response.get("msg") != channel:
            raise RuntimeError("MEXC spot private stream subscription failed")

    async def _read_spot(self) -> None:
        try:
            while True:
                raw = await self._spot_socket.recv()
                if isinstance(raw, bytes):
                    await self._events.put(raw)
                    continue
                event = self._decode(raw)
                if event.get("msg") == "PONG":
                    if self._spot_pong is not None and not self._spot_pong.done():
                        self._spot_pong.set_result(None)
                    continue
                if event.get("code") not in (None, 0):
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _read_futures(self) -> None:
        try:
            while True:
                event = self._decode(await self._futures_socket.recv())
                if event.get("channel") == "pong":
                    if (
                        self._futures_pong is not None
                        and not self._futures_pong.done()
                    ):
                        self._futures_pong.set_result(None)
                    continue
                if event.get("channel") == "rs.error":
                    await self._events.put(_StreamFailure())
                    return
                await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(30 * 60)
            if not self._listen_key:
                await self._events.put(_StreamFailure())
                return
            response = await self._client().put(
                "/api/v3/userDataStream",
                params={"listenKey": self._listen_key},
                headers={"X-MEXC-APIKEY": self.secrets.api_key},
            )
            if not response.is_success:
                await self._events.put(_StreamFailure())
                return
            try:
                listen_key = str(response.json()["listenKey"])
            except (KeyError, TypeError, ValueError):
                await self._events.put(_StreamFailure())
                return
            if listen_key != self._listen_key:
                await self._events.put(_StreamFailure())
                return

    def _client(self) -> httpx.AsyncClient:
        if self._spot_client is None or self._spot_client.is_closed:
            self._spot_client = httpx.AsyncClient(
                base_url="https://api.mexc.com",
                timeout=self.timeout_seconds,
            )
        return self._spot_client

    async def _receive_json(self, socket: Any) -> dict[str, Any]:
        return self._decode(
            await asyncio.wait_for(
                socket.recv(),
                timeout=self.timeout_seconds,
            )
        )

    @staticmethod
    def _decode(value: str | bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("MEXC private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("MEXC private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
