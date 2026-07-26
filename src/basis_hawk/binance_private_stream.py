from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from websockets.asyncio.client import connect as websocket_connect

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class BinancePrivateStreamConnection:
    exchange = Exchange.BINANCE
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
        futures_client: httpx.AsyncClient | None = None,
        connector: Callable[..., Any] = websocket_connect,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._futures_client = futures_client
        self._owned_futures_client = futures_client is None
        self._connector = connector
        self._spot_socket: Any | None = None
        self._futures_socket: Any | None = None
        self._listen_key: str | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def _spot_url(self) -> str:
        if self.environment == ExchangeEnvironment.SANDBOX:
            return "wss://ws-api.testnet.binance.vision/ws-api/v3"
        return "wss://ws-api.binance.com:443/ws-api/v3"

    @property
    def _futures_rest_url(self) -> str:
        if self.environment == ExchangeEnvironment.SANDBOX:
            return "https://demo-fapi.binance.com"
        return "https://fapi.binance.com"

    @property
    def _futures_stream_url(self) -> str:
        if self.environment == ExchangeEnvironment.SANDBOX:
            return "wss://demo-fstream.binance.com/private"
        return "wss://fstream.binance.com/private"

    async def connect(self) -> None:
        await self.close()
        while not self._events.empty():
            self._events.get_nowait()
        try:
            self._spot_socket = await self._connector(
                self._spot_url,
                ping_interval=10,
                ping_timeout=10,
                close_timeout=5,
            )
            request_id = str(uuid.uuid4())
            params: dict[str, object] = {
                "apiKey": self.secrets.api_key,
                "timestamp": self.clock_ms(),
            }
            payload = "&".join(
                f"{key}={value}" for key, value in sorted(params.items())
            )
            params["signature"] = hmac.new(
                self.secrets.api_secret.encode(),
                payload.encode(),
                hashlib.sha256,
            ).hexdigest()
            await self._spot_socket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "userDataStream.subscribe.signature",
                        "params": params,
                    }
                )
            )
            response = self._decode(
                await asyncio.wait_for(
                    self._spot_socket.recv(),
                    timeout=self.timeout_seconds,
                )
            )
            if (
                response.get("id") != request_id
                or response.get("status") != 200
                or not isinstance(response.get("result"), dict)
                or response["result"].get("subscriptionId") is None
            ):
                raise RuntimeError("Binance spot private stream authentication failed")

            client = self._client()
            listen_response = await client.post(
                "/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": self.secrets.api_key},
            )
            if not listen_response.is_success:
                raise RuntimeError("Binance futures private stream authentication failed")
            try:
                listen_key = str(listen_response.json()["listenKey"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Binance futures private stream authentication failed"
                ) from exc
            if not listen_key:
                raise RuntimeError("Binance futures private stream authentication failed")
            self._listen_key = listen_key
            self._futures_socket = await self._connector(
                f"{self._futures_stream_url}/ws/{listen_key}",
                ping_interval=10,
                ping_timeout=10,
                close_timeout=5,
            )
            self._reader_tasks = [
                asyncio.create_task(self._read(self._spot_socket)),
                asyncio.create_task(self._read(self._futures_socket)),
            ]
            self._keepalive_task = asyncio.create_task(self._keepalive())
        except Exception:
            await self.close()
            raise

    async def receive(self) -> object:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise RuntimeError("Binance private event stream closed")
        return event

    async def probe(self) -> None:
        if self._spot_socket is None or self._futures_socket is None:
            raise RuntimeError("Binance private event stream is not connected")
        for socket in (self._spot_socket, self._futures_socket):
            pong = await socket.ping()
            await asyncio.wait_for(pong, timeout=self.timeout_seconds)

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
        for socket in (self._spot_socket, self._futures_socket):
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    pass
        self._spot_socket = None
        self._futures_socket = None
        self._listen_key = None
        if (
            self._owned_futures_client
            and self._futures_client is not None
            and not self._futures_client.is_closed
        ):
            await self._futures_client.aclose()

    async def _read(self, socket: Any) -> None:
        try:
            while True:
                await self._events.put(self._decode(await socket.recv()))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(_StreamFailure())

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(30 * 60)
            client = self._client()
            response = await client.post(
                "/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": self.secrets.api_key},
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
        if self._futures_client is None or self._futures_client.is_closed:
            self._futures_client = httpx.AsyncClient(
                base_url=self._futures_rest_url,
                timeout=self.timeout_seconds,
            )
        return self._futures_client

    @staticmethod
    def _decode(value: str | bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Binance private stream sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Binance private stream sent an invalid event")
        return decoded


class _StreamFailure:
    pass
