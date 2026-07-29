import asyncio
import json

import httpx

from basis_hawk.binance_private_stream import BinancePrivateStreamConnection
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets


class FakeSocket:
    def __init__(self, initial: list[dict[str, object]]) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for item in initial:
            self.incoming.put_nowait(json.dumps(item))
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.pings = 0

    async def send(self, value: str) -> None:
        request = json.loads(value)
        self.sent.append(request)
        if request["method"] == "userDataStream.subscribe.signature":
            await self.incoming.put(
                json.dumps(
                    {
                        "id": request["id"],
                        "status": 200,
                        "result": {"subscriptionId": 0},
                    }
                )
            )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def ping(self) -> asyncio.Future[None]:
        self.pings += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def close(self) -> None:
        self.closed = True


async def test_binance_connects_signed_spot_and_listen_key_futures_streams() -> None:
    spot = FakeSocket([])
    futures = FakeSocket([{"e": "ORDER_TRADE_UPDATE"}])
    sockets = [spot, futures]
    urls: list[str] = []

    async def connector(url: str, **options: object) -> FakeSocket:
        assert options["ping_interval"] == 10
        urls.append(url)
        return sockets.pop(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-MBX-APIKEY"] == "test-api-key"
        assert request.url.path == "/fapi/v1/listenKey"
        return httpx.Response(200, json={"listenKey": "futures-key"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://fapi.binance.com",
    )
    connection = BinancePrivateStreamConnection(
        ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        futures_client=http,
        connector=connector,
    )

    await connection.connect()
    await spot.incoming.put(
        json.dumps({"subscriptionId": 0, "event": {"e": "executionReport"}})
    )

    request = spot.sent[0]
    assert request["method"] == "userDataStream.subscribe.signature"
    assert request["params"] == {
        "apiKey": "test-api-key",
        "timestamp": 1_700_000_000_000,
        "signature": "1e234bd50ee1bd767a16c4d26463cd5a3ed3e868c460637a12b0dbabbea2f43d",
    }
    assert urls == [
        "wss://ws-api.binance.com:443/ws-api/v3",
        "wss://fstream.binance.com/private/ws/futures-key",
    ]
    events = [await connection.receive(), await connection.receive()]
    assert {item.get("e") or item["event"]["e"] for item in events} == {
        "executionReport",
        "ORDER_TRADE_UPDATE",
    }
    await connection.probe()
    assert spot.pings == 1
    assert futures.pings == 1
    await connection.close()
    assert spot.closed is True
    assert futures.closed is True
    await http.aclose()


async def test_binance_sandbox_uses_current_demo_endpoints() -> None:
    connection = BinancePrivateStreamConnection(
        ExchangeSecrets(api_key="test-api-key", api_secret="test-api-secret"),
        ExchangeEnvironment.SANDBOX,
    )

    assert connection._spot_url == (  # noqa: SLF001
        "wss://demo-ws-api.binance.com/ws-api/v3"
    )
    assert connection._futures_rest_url == "https://demo-fapi.binance.com"  # noqa: SLF001
    assert connection._futures_stream_url == (  # noqa: SLF001
        "wss://demo-fstream.binance.com/private"
    )
