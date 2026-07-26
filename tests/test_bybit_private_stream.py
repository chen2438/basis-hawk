import asyncio
import json

from basis_hawk.bybit_private_stream import BybitPrivateStreamConnection
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, value: str) -> None:
        request = json.loads(value)
        self.sent.append(request)
        if request["op"] == "auth":
            await self.incoming.put(
                json.dumps(
                    {
                        "success": True,
                        "ret_msg": "",
                        "op": "auth",
                        "conn_id": "test",
                    }
                )
            )
        elif request["op"] == "subscribe":
            await self.incoming.put(
                json.dumps(
                    {
                        "success": True,
                        "ret_msg": "",
                        "op": "subscribe",
                        "conn_id": "test",
                    }
                )
            )
        elif request["op"] == "ping":
            await self.incoming.put(
                json.dumps(
                    {
                        "req_id": request["req_id"],
                        "op": "pong",
                        "args": ["1700000000000"],
                        "conn_id": "test",
                    }
                )
            )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


async def test_bybit_authenticates_subscribes_and_pings() -> None:
    socket = FakeSocket()
    urls: list[str] = []

    async def connector(url: str, **options: object) -> FakeSocket:
        assert options == {"ping_interval": None, "close_timeout": 5}
        urls.append(url)
        return socket

    connection = BybitPrivateStreamConnection(
        ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        connector=connector,
    )

    await connection.connect()

    assert socket.sent[0] == {
        "req_id": "basisHawkAuth",
        "op": "auth",
        "args": [
            "test-api-key",
            1_700_000_001_000,
            "76f5035b9ff3e8c8d99639a2c34a6b9aa5991061395e80dbc6edc76c0a93c2a1",
        ],
    }
    assert socket.sent[1] == {
        "req_id": "basisHawkPrivate",
        "op": "subscribe",
        "args": ["order", "execution", "position", "wallet"],
    }
    assert urls == ["wss://stream.bybit.com/v5/private"]

    await socket.incoming.put(
        json.dumps(
            {
                "id": "event",
                "topic": "execution",
                "creationTime": 1_700_000_000_000,
                "data": [{"execId": "1"}],
            }
        )
    )
    event = await connection.receive()
    assert event["topic"] == "execution"
    await connection.probe()
    assert socket.sent[-1] == {
        "req_id": "basisHawkPing",
        "op": "ping",
    }
    await connection.close()
    assert socket.closed is True


def test_bybit_sandbox_uses_testnet_private_endpoint() -> None:
    connection = BybitPrivateStreamConnection(
        ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        ExchangeEnvironment.SANDBOX,
    )

    assert connection.url == "wss://stream-testnet.bybit.com/v5/private"
