import asyncio
import json

import pytest

from basis_hawk.bitget_private_stream import BitgetPrivateStreamConnection
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)


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
                json.dumps({"event": "login", "code": "0", "msg": ""})
            )
        elif request["op"] == "subscribe":
            for argument in request["args"]:
                await self.incoming.put(
                    json.dumps(
                        {
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


@pytest.mark.parametrize(
    ("generation", "environment", "expected_url", "expected_subscriptions"),
    [
        (
            "uta",
            ExchangeEnvironment.LIVE,
            "wss://ws.bitget.com/v3/ws/private",
            [
                {"instType": "UTA", "topic": "order"},
                {"instType": "UTA", "topic": "fill"},
                {"instType": "UTA", "topic": "position"},
                {"instType": "UTA", "topic": "account"},
            ],
        ),
        (
            "classic",
            ExchangeEnvironment.SANDBOX,
            "wss://wspap.bitget.com/v2/ws/private",
            [
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
                {
                    "instType": "SPOT",
                    "channel": "account",
                    "coin": "default",
                },
                {
                    "instType": "USDT-FUTURES",
                    "channel": "account",
                    "coin": "default",
                },
            ],
        ),
    ],
)
async def test_bitget_resolves_generation_before_login_and_subscription(
    generation: str,
    environment: ExchangeEnvironment,
    expected_url: str,
    expected_subscriptions: list[dict[str, str]],
) -> None:
    socket = FakeSocket()
    urls: list[str] = []

    async def resolver() -> str:
        return generation

    async def connector(url: str, **options: object) -> FakeSocket:
        assert options == {"ping_interval": None, "close_timeout": 5}
        urls.append(url)
        return socket

    connection = BitgetPrivateStreamConnection(
        SECRETS,
        environment,
        clock_ms=lambda: 1_700_000_000_000,
        generation_resolver=resolver,
        connector=connector,
    )

    await connection.connect()

    assert urls == [expected_url]
    assert socket.sent[0] == {
        "op": "login",
        "args": [
            {
                "apiKey": "test-api-key",
                "passphrase": "test-passphrase",
                "timestamp": "1700000000",
                    "sign": "X3EWQeL5AwAlQUbSLbUp/bShEUaFiRE5JYOIo1BG0Vw=",
            }
        ],
    }
    subscribe = socket.sent[1]
    assert isinstance(subscribe, dict)
    assert subscribe["args"] == expected_subscriptions

    await socket.incoming.put(
        json.dumps(
            {
                "arg": expected_subscriptions[0],
                "data": [{"orderId": "1"}],
            }
        )
    )
    assert (await connection.receive())["data"][0]["orderId"] == "1"
    await connection.probe()
    assert socket.sent[-1] == "ping"
    await connection.close()
    assert socket.closed is True
