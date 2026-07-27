import asyncio
import hashlib
import hmac
import json

import pytest

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.gate_private_stream import GatePrivateStreamConnection

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
)


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.ping_count = 0

    async def send(self, value: str) -> None:
        request = json.loads(value)
        self.sent.append(request)
        await self.incoming.put(
            json.dumps(
                {
                    "time": request["time"],
                    "channel": request["channel"],
                    "event": request["event"],
                    "error": None,
                    "result": {"status": "success"},
                }
            )
        )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def ping(self) -> asyncio.Future[None]:
        self.ping_count += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def close(self) -> None:
        self.closed = True


async def test_gate_subscribes_spot_and_futures_private_channels() -> None:
    spot = FakeSocket()
    futures = FakeSocket()
    sockets = iter((spot, futures))
    calls: list[tuple[str, dict[str, object]]] = []

    async def resolver() -> str:
        return "20011"

    async def connector(url: str, **options: object) -> FakeSocket:
        calls.append((url, options))
        return next(sockets)

    connection = GatePrivateStreamConnection(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_s=lambda: 1_700_000_000,
        user_id_resolver=resolver,
        connector=connector,
    )

    await connection.connect()

    assert calls == [
        (
            "wss://api.gateio.ws/ws/v4/",
            {"ping_interval": None, "close_timeout": 5},
        ),
        (
            "wss://fx-ws.gateio.ws/v4/ws/usdt",
            {
                "ping_interval": None,
                "close_timeout": 5,
                "additional_headers": {"X-Gate-Size-Decimal": "1"},
            },
        ),
    ]
    assert [
        (request["channel"], request["payload"])
        for request in spot.sent + futures.sent
    ] == [
        ("spot.orders", ["!all"]),
        ("spot.usertrades", ["!all"]),
        ("futures.orders", ["20011", "!all"]),
        ("futures.usertrades", ["20011", "!all"]),
        ("futures.positions", ["20011", "!all"]),
    ]
    for request in spot.sent + futures.sent:
        channel = str(request["channel"])
        expected_signature = hmac.new(
            b"test-api-secret",
            (
                f"channel={channel}&event=subscribe&time=1700000000"
            ).encode(),
            hashlib.sha512,
        ).hexdigest()
        assert request["auth"] == {
            "method": "api_key",
            "KEY": "test-api-key",
            "SIGN": expected_signature,
        }

    await spot.incoming.put(
        json.dumps(
            {
                "channel": "spot.orders",
                "event": "update",
                "result": [{"id": "1"}],
            }
        )
    )
    assert (await connection.receive())["result"][0]["id"] == "1"
    await futures.incoming.put(
        json.dumps(
            {
                "channel": "futures.positions",
                "event": "update",
                "result": [{"contract": "ORDER_USDT"}],
            }
        )
    )
    assert (await connection.receive())["result"][0]["contract"] == "ORDER_USDT"

    await connection.probe()
    assert spot.ping_count == 1
    assert futures.ping_count == 1
    await connection.close()
    assert spot.closed is True
    assert futures.closed is True


async def test_gate_sandbox_uses_spot_and_futures_testnet_channels() -> None:
    spot = FakeSocket()
    futures = FakeSocket()
    sockets = iter((spot, futures))
    calls: list[tuple[str, dict[str, object]]] = []

    async def connector(url: str, **options: object) -> FakeSocket:
        calls.append((url, options))
        return next(sockets)

    connection = GatePrivateStreamConnection(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        user_id_resolver=lambda: asyncio.sleep(0, result="20011"),
        connector=connector,
    )

    await connection.connect()

    assert calls == [
        (
            "wss://ws-testnet.gate.com/v4/ws/spot",
            {"ping_interval": None, "close_timeout": 5},
        ),
        (
            "wss://ws-testnet.gate.com/v4/ws/futures/usdt",
            {
                "ping_interval": None,
                "close_timeout": 5,
                "additional_headers": {"X-Gate-Size-Decimal": "1"},
            },
        ),
    ]
    await connection.close()


async def test_gate_closes_both_channels_when_one_reader_fails() -> None:
    spot = FakeSocket()
    futures = FakeSocket()
    sockets = iter((spot, futures))

    async def resolver() -> str:
        return "20011"

    async def connector(url: str, **options: object) -> FakeSocket:
        return next(sockets)

    connection = GatePrivateStreamConnection(
        SECRETS,
        ExchangeEnvironment.LIVE,
        user_id_resolver=resolver,
        connector=connector,
    )
    await connection.connect()
    await spot.incoming.put("not-json")

    with pytest.raises(RuntimeError, match="closed"):
        await connection.receive()

    await connection.close()
    assert spot.closed is True
    assert futures.closed is True
