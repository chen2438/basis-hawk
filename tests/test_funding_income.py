from datetime import UTC, datetime
from decimal import Decimal

import httpx

from basis_hawk.accounts import (
    BinanceAccountClient,
    BitgetAccountClient,
    BybitAccountClient,
    GateAccountClient,
    MexcAccountClient,
    OkxAccountClient,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.storage import Database

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)
SINCE = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


async def test_binance_and_okx_funding_income_is_normalized() -> None:
    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[])
        ),
        base_url="https://spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "tranId": 11,
                        "symbol": "ORDERUSDT",
                        "asset": "USDT",
                        "income": "0.25",
                        "time": 1785087000000,
                    }
                ],
            )
        ),
        base_url="https://perp.test",
    )
    binance = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=spot,
        perp_client=perp,
    )
    batch = await binance.funding_income(since=SINCE)
    assert batch.complete is True
    assert batch.records[0].base_asset == "ORDER"
    assert batch.records[0].amount == Decimal("0.25")
    await spot.aclose()
    await perp.aclose()

    okx_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "billId": "21",
                            "subType": "174",
                            "instId": "ORDER-USDT-SWAP",
                            "ccy": "USDT",
                            "balChg": "-0.1",
                            "ts": "1785087000000",
                        }
                    ],
                },
            )
        ),
        base_url="https://okx.test",
    )
    okx = OkxAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=okx_http,
    )
    batch = await okx.funding_income(since=SINCE)
    assert batch.records[0].base_asset == "ORDER"
    assert batch.records[0].amount == Decimal("-0.1")
    await okx_http.aclose()


async def test_bybit_and_bitget_funding_income_is_normalized() -> None:
    bybit_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "id": "31",
                                "symbol": "ORDERUSDT",
                                "currency": "USDT",
                                "funding": "0.2",
                                "feeRate": "0.0001",
                                "transactionTime": "1785087000000",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )
        ),
        base_url="https://bybit.test",
    )
    bybit = BybitAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=bybit_http,
    )
    batch = await bybit.funding_income(since=SINCE)
    assert batch.records[0].rate == Decimal("0.0001")
    assert batch.records[0].amount == Decimal("0.2")
    await bybit_http.aclose()

    bitget_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": {
                        "bills": [
                            {
                                "billId": "41",
                                "symbol": "ORDERUSDT",
                                "coin": "USDT",
                                "amount": "-0.3",
                                "cTime": "1785087000000",
                            }
                        ],
                        "endId": "",
                    },
                },
            )
        ),
        base_url="https://bitget.test",
    )
    bitget = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=bitget_http,
    )
    bitget._account_generation = "classic"
    batch = await bitget.funding_income(since=SINCE)
    assert batch.records[0].base_asset == "ORDER"
    assert batch.records[0].amount == Decimal("-0.3")
    await bitget_http.aclose()


async def test_gate_and_mexc_funding_income_is_normalized() -> None:
    def gate_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "fund"
        assert "from" in request.url.params
        return httpx.Response(
            200,
            json=[
                {
                    "id": "51",
                    "contract": "ORDER_USDT",
                    "change": "0.4",
                    "time": 1785087000,
                }
            ],
        )

    gate_http = httpx.AsyncClient(
        transport=httpx.MockTransport(gate_handler),
        base_url="https://gate.test",
    )
    gate = GateAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=gate_http,
    )
    batch = await gate.funding_income(since=SINCE)
    assert batch.records[0].base_asset == "ORDER"
    assert batch.records[0].amount == Decimal("0.4")
    await gate_http.aclose()

    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        ),
        base_url="https://mexc-spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {
                        "totalCount": 1,
                        "resultList": [
                            {
                                "id": "61",
                                "symbol": "ORDER_USDT",
                                "funding": "-0.5",
                                "rate": "0.0002",
                                "positionValue": "100",
                                "settleTime": 1785087000000,
                            }
                        ],
                    },
                },
            )
        ),
        base_url="https://mexc-perp.test",
    )
    mexc = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=spot,
        perp_client=perp,
    )
    batch = await mexc.funding_income(since=SINCE)
    assert batch.records[0].position_value == Decimal("100")
    assert batch.records[0].amount == Decimal("-0.5")
    await spot.aclose()
    await perp.aclose()


async def test_funding_income_storage_is_idempotent_and_bounded() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    records = [
        {
            "exchange_record_id": "fund-1",
            "symbol": "ORDERUSDT",
            "base_asset": "ORDER",
            "asset": "USDT",
            "amount": Decimal("0.25"),
            "rate": Decimal("0.0001"),
            "position_value": Decimal("100"),
            "occurred_at": SINCE,
        }
    ]
    assert (
        await database.persist_funding_income(
            exchange="binance",
            environment="live",
            records=records,
        )
        == 1
    )
    assert (
        await database.persist_funding_income(
            exchange="binance",
            environment="live",
            records=records,
        )
        == 0
    )
    rows = await database.list_funding_income(limit=1)
    assert rows[0].amount == Decimal("0.25")
    assert (
        await database.latest_funding_income_at(
            exchange="binance",
            environment="live",
        )
        == SINCE
    )
    await database.close()
