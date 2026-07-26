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

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)
SINCE = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


async def test_binance_and_okx_order_fills_are_normalized() -> None:
    binance_spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "orderId": 22,
                        "symbol": "ORDERUSDT",
                        "price": "0.05",
                        "qty": "20",
                        "commission": "0.001",
                        "commissionAsset": "ORDER",
                        "time": 1785087000000,
                        "isBuyer": True,
                        "isMaker": False,
                    }
                ],
            )
        ),
        base_url="https://spot.test",
    )
    binance_perp = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[])
        ),
        base_url="https://perp.test",
    )
    binance = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=binance_spot,
        perp_client=binance_perp,
    )
    batch = await binance.fills_for_order(
        market="spot",
        symbol="ORDERUSDT",
        exchange_order_id="22",
        client_order_id="bh-order-s",
        since=SINCE,
    )
    assert batch.complete is True
    assert batch.fills[0].exchange_trade_id == "11"
    assert batch.fills[0].client_order_id == "bh-order-s"
    assert batch.fills[0].side == "buy"
    assert batch.fills[0].fee_amount == Decimal("0.001")
    await binance_spot.aclose()
    await binance_perp.aclose()

    okx_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "tradeId": "31",
                            "ordId": "32",
                            "clOrdId": "bh-order-p",
                            "instId": "ORDER-USDT-SWAP",
                            "side": "sell",
                            "fillPx": "0.051",
                            "fillSz": "20",
                            "fee": "-0.002",
                            "feeCcy": "USDT",
                            "execType": "T",
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
        clock=lambda: SINCE,
        client=okx_http,
    )
    batch = await okx.fills_for_order(
        market="perp",
        symbol="ORDER-USDT-SWAP",
        exchange_order_id="32",
        client_order_id="bh-order-p",
        since=SINCE,
    )
    assert batch.fills[0].fee_amount == Decimal("0.002")
    assert batch.fills[0].liquidity == "taker"
    await okx_http.aclose()


async def test_bybit_and_bitget_order_fills_are_normalized() -> None:
    bybit_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "execId": "41",
                                "orderId": "42",
                                "orderLinkId": "bh-order-p",
                                "symbol": "ORDERUSDT",
                                "side": "Sell",
                                "execPrice": "0.051",
                                "execQty": "20",
                                "execFee": "0.002",
                                "feeCurrency": "USDT",
                                "isMaker": False,
                                "execTime": "1785087000000",
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
        clock_ms=lambda: 1785088000000,
        client=bybit_http,
    )
    batch = await bybit.fills_for_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id=None,
        client_order_id="bh-order-p",
        since=SINCE,
    )
    assert batch.complete is True
    assert batch.fills[0].exchange_order_id == "42"
    assert batch.fills[0].side == "sell"
    await bybit_http.aclose()

    def bitget_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spot/trade/fills"):
            data: object = [
                {
                    "tradeId": "51",
                    "orderId": "52",
                    "symbol": "ORDERUSDT",
                    "side": "buy",
                    "priceAvg": "0.05",
                    "size": "20",
                    "feeDetail": {
                        "feeCoin": "ORDER",
                        "totalFee": "-0.001",
                    },
                    "tradeScope": "taker",
                    "cTime": "1785087000000",
                }
            ]
        else:
            data = {
                "fillList": [
                    {
                        "tradeId": "53",
                        "orderId": "54",
                        "symbol": "ORDERUSDT",
                        "side": "sell",
                        "price": "0.051",
                        "baseVolume": "20",
                        "feeDetail": [
                            {"feeCoin": "USDT", "totalFee": "-0.002"}
                        ],
                        "tradeScope": "maker",
                        "cTime": "1785087000000",
                    }
                ]
            }
        return httpx.Response(200, json={"code": "00000", "data": data})

    bitget_http = httpx.AsyncClient(
        transport=httpx.MockTransport(bitget_handler),
        base_url="https://bitget.test",
    )
    bitget = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        client=bitget_http,
    )
    spot_batch = await bitget.fills_for_order(
        market="spot",
        symbol="ORDERUSDT",
        exchange_order_id="52",
        client_order_id="bh-order-s",
        since=SINCE,
    )
    perp_batch = await bitget.fills_for_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id="54",
        client_order_id="bh-order-p",
        since=SINCE,
    )
    assert spot_batch.fills[0].fee_amount == Decimal("0.001")
    assert spot_batch.fills[0].fee_asset == "ORDER"
    assert perp_batch.fills[0].liquidity == "maker"
    await bitget_http.aclose()


async def test_gate_and_mexc_order_fills_are_normalized() -> None:
    def gate_handler(request: httpx.Request) -> httpx.Response:
        if "/spot/" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "61",
                        "order_id": "62",
                        "currency_pair": "ORDER_USDT",
                        "side": "buy",
                        "amount": "20",
                        "price": "0.05",
                        "fee": "0.001",
                        "fee_currency": "ORDER",
                        "role": "taker",
                        "create_time_ms": "1785087000000",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": "63",
                    "order_id": "64",
                    "contract": "ORDER_USDT",
                    "size": -20,
                    "price": "0.051",
                    "fee": "0.002",
                    "role": "maker",
                    "create_time": 1785087000,
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
        clock_s=lambda: 1785088000,
        client=gate_http,
    )
    spot_batch = await gate.fills_for_order(
        market="spot",
        symbol="ORDER_USDT",
        exchange_order_id="62",
        client_order_id="bh-order-s",
        since=SINCE,
    )
    perp_batch = await gate.fills_for_order(
        market="perp",
        symbol="ORDER_USDT",
        exchange_order_id="64",
        client_order_id="bh-order-p",
        since=SINCE,
    )
    assert spot_batch.fills[0].occurred_at.year == 2026
    assert perp_batch.fills[0].side == "sell"
    assert perp_batch.fills[0].quantity == Decimal("20")
    await gate_http.aclose()

    mexc_spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "id": "71",
                        "orderId": "72",
                        "symbol": "ORDERUSDT",
                        "price": "0.05",
                        "qty": "20",
                        "commission": "0.001",
                        "commissionAsset": "ORDER",
                        "time": 1785087000000,
                        "isBuyer": True,
                        "isMaker": False,
                    }
                ],
            )
        ),
        base_url="https://mexc-spot.test",
    )
    mexc_perp = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "73",
                            "orderId": "74",
                            "symbol": "ORDER_USDT",
                            "side": 3,
                            "vol": "20",
                            "price": "0.051",
                            "fee": "0.002",
                            "feeCurrency": "USDT",
                            "timestamp": 1785087000000,
                            "taker": True,
                        }
                    ],
                },
            )
        ),
        base_url="https://mexc-perp.test",
    )
    mexc = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=mexc_spot,
        perp_client=mexc_perp,
    )
    spot_batch = await mexc.fills_for_order(
        market="spot",
        symbol="ORDERUSDT",
        exchange_order_id="72",
        client_order_id="bh-order-s",
        since=SINCE,
    )
    perp_batch = await mexc.fills_for_order(
        market="perp",
        symbol="ORDER_USDT",
        exchange_order_id="74",
        client_order_id="bh-order-p",
        since=SINCE,
    )
    assert spot_batch.fills[0].side == "buy"
    assert perp_batch.fills[0].side == "sell"
    assert perp_batch.fills[0].liquidity == "taker"
    await mexc_spot.aclose()
    await mexc_perp.aclose()


async def test_fill_reconciliation_reports_missing_exchange_order_id() -> None:
    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
        base_url="https://spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=spot,
        perp_client=perp,
    )
    batch = await client.fills_for_order(
        market="spot",
        symbol="ORDERUSDT",
        exchange_order_id=None,
        client_order_id="bh-order-s",
        since=SINCE,
    )
    assert batch.complete is False
    assert "exchange order ID" in (batch.incomplete_reason or "")
    await spot.aclose()
    await perp.aclose()
