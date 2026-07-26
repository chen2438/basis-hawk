import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.mexc_private_stream import MexcPrivateStreamConnection

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
)


class FakeSocket:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, value: str) -> None:
        request = json.loads(value)
        self.sent.append(request)
        method = request["method"]
        if self.kind == "spot" and method == "SUBSCRIPTION":
            await self.incoming.put(
                json.dumps(
                    {
                        "id": 0,
                        "code": 0,
                        "msg": request["params"][0],
                    }
                )
            )
        elif self.kind == "spot" and method == "PING":
            await self.incoming.put(
                json.dumps({"id": 0, "code": 0, "msg": "PONG"})
            )
        elif self.kind == "futures" and method == "login":
            await self.incoming.put(
                json.dumps(
                    {
                        "channel": "rs.login",
                        "data": "success",
                        "ts": "1700000000000",
                    }
                )
            )
        elif self.kind == "futures" and method == "ping":
            await self.incoming.put(
                json.dumps({"channel": "pong", "data": 1_700_000_000_000})
            )

    async def recv(self) -> str | bytes:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


async def test_mexc_connects_spot_and_futures_private_streams() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"listenKey": "test-listen-key"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mexc.test",
    )
    spot = FakeSocket("spot")
    futures = FakeSocket("futures")
    sockets = iter((spot, futures))
    calls: list[tuple[str, dict[str, object]]] = []

    async def connector(url: str, **options: object) -> FakeSocket:
        calls.append((url, options))
        return next(sockets)

    connection = MexcPrivateStreamConnection(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        spot_client=http,
        connector=connector,
    )

    await connection.connect()

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v3/userDataStream"
    assert requests[0].headers["X-MEXC-APIKEY"] == "test-api-key"
    assert calls == [
        (
            "wss://wbs-api.mexc.com/ws?listenKey=test-listen-key",
            {"ping_interval": None, "close_timeout": 5},
        ),
        (
            "wss://contract.mexc.com/edge",
            {"ping_interval": None, "close_timeout": 5},
        ),
    ]
    assert [request["params"][0] for request in spot.sent] == [
        "spot@private.orders.v3.api.pb",
        "spot@private.deals.v3.api.pb",
        "spot@private.account.v3.api.pb",
    ]
    login = futures.sent[0]
    expected_signature = hmac.new(
        b"test-api-secret",
        b"test-api-key1700000000000",
        hashlib.sha256,
    ).hexdigest()
    assert login == {
        "method": "login",
        "param": {
            "apiKey": "test-api-key",
            "reqTime": "1700000000000",
            "signature": expected_signature,
        },
    }

    await spot.incoming.put(b"\x08\x01")
    assert await connection.receive() == b"\x08\x01"
    await futures.incoming.put(
        json.dumps(
            {
                "channel": "push.personal.position",
                "data": {"symbol": "ORDER_USDT"},
            }
        )
    )
    assert (await connection.receive())["data"]["symbol"] == "ORDER_USDT"

    await connection.probe()
    assert spot.sent[-1] == {"method": "PING"}
    assert futures.sent[-1] == {"method": "ping"}
    await connection.close()
    assert requests[-1].method == "DELETE"
    assert requests[-1].url.params["listenKey"] == "test-listen-key"
    assert spot.closed is True
    assert futures.closed is True
    await http.aclose()


async def test_mexc_rejects_sandbox_before_network_access() -> None:
    connected = False

    async def connector(url: str, **options: object) -> FakeSocket:
        nonlocal connected
        connected = True
        return FakeSocket("spot")

    connection = MexcPrivateStreamConnection(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        connector=connector,
    )

    with pytest.raises(RuntimeError, match="live environment"):
        await connection.connect()

    assert connected is False
