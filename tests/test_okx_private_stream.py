import asyncio
import json

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.okx_private_stream import OkxPrivateStreamConnection


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str | dict[str, object]] = []
        self.closed = False

    async def send(self, value: str) -> None:
        if value == "ping":
            self.sent.append(value)
            await self.incoming.put("pong")
            return
        request = json.loads(value)
        self.sent.append(request)
        if request["op"] == "login":
            await self.incoming.put(
                json.dumps(
                    {
                        "event": "login",
                        "code": "0",
                        "msg": "",
                        "connId": "test",
                    }
                )
            )
        elif request["op"] == "subscribe":
            for argument in request["args"]:
                await self.incoming.put(
                    json.dumps(
                        {
                            "id": request["id"],
                            "event": "subscribe",
                            "arg": argument,
                            "connId": "test",
                        }
                    )
                )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


async def test_okx_authenticates_subscribes_and_uses_text_ping() -> None:
    socket = FakeSocket()
    urls: list[str] = []

    async def connector(url: str, **options: object) -> FakeSocket:
        assert options == {"ping_interval": None, "close_timeout": 5}
        urls.append(url)
        return socket

    connection = OkxPrivateStreamConnection(
        ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
            passphrase="test-passphrase",
        ),
        ExchangeEnvironment.LIVE,
        clock_seconds=lambda: 1_700_000_000,
        connector=connector,
    )

    await connection.connect()

    login = socket.sent[0]
    assert isinstance(login, dict)
    assert login == {
        "op": "login",
        "args": [
            {
                "apiKey": "test-api-key",
                "passphrase": "test-passphrase",
                "timestamp": "1700000000",
                "sign": "9uuEETDkqnWE85ZTmYBZFWcn1kUYRHTrrEDCTa0Qfxo=",
            }
        ],
    }
    subscribe = socket.sent[1]
    assert isinstance(subscribe, dict)
    assert subscribe["args"] == [
        {"channel": "orders", "instType": "ANY"},
        {"channel": "positions", "instType": "ANY"},
        {"channel": "account"},
    ]
    assert urls == ["wss://ws.okx.com:8443/ws/v5/private"]

    await socket.incoming.put(
        json.dumps(
            {
                "arg": {"channel": "orders", "instType": "SPOT"},
                "data": [{"ordId": "1"}],
            }
        )
    )
    event = await connection.receive()
    assert event["arg"]["channel"] == "orders"
    await connection.probe()
    assert socket.sent[-1] == "ping"
    await connection.close()
    assert socket.closed is True


def test_okx_sandbox_uses_demo_private_endpoint() -> None:
    connection = OkxPrivateStreamConnection(
        ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
            passphrase="test-passphrase",
        ),
        ExchangeEnvironment.SANDBOX,
    )

    assert connection.url == "wss://wspap.okx.com:8443/ws/v5/private"
