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


async def test_binance_and_okx_orders_are_found_by_client_id() -> None:
    binance_spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "symbol": "ORDERUSDT",
                    "orderId": 12,
                    "clientOrderId": "bh-order-s",
                    "price": "0.05",
                    "origQty": "20",
                    "executedQty": "5",
                    "status": "PARTIALLY_FILLED",
                    "side": "BUY",
                },
            )
        ),
        base_url="https://spot.test",
    )
    binance_perp = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="https://perp.test",
    )
    binance = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=binance_spot,
        perp_client=binance_perp,
    )

    lookup = await binance.order_by_client_id(
        market="spot",
        symbol="ORDERUSDT",
        client_order_id="bh-order-s",
    )

    assert lookup.complete is True
    assert lookup.order is not None
    assert lookup.order.exchange_order_id == "12"
    assert lookup.order.filled_quantity == Decimal("5")
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
                            "ordId": "22",
                            "clOrdId": "bh-order-p",
                            "instId": "ORDER-USDT-SWAP",
                            "side": "sell",
                            "state": "filled",
                            "px": "0.051",
                            "avgPx": "0.0509",
                            "sz": "20",
                            "accFillSz": "20",
                            "reduceOnly": "false",
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
        clock=lambda: datetime(2026, 7, 26, 18, 0, tzinfo=UTC),
        client=okx_http,
    )

    lookup = await okx.order_by_client_id(
        market="perp",
        symbol="ORDER-USDT-SWAP",
        client_order_id="bh-order-p",
    )

    assert lookup.order is not None
    assert lookup.order.exchange_order_id == "22"
    assert lookup.order.side == "sell"
    assert lookup.order.reduce_only is False
    await okx_http.aclose()


async def test_bybit_history_fallback_and_bitget_orders_are_normalized() -> None:
    bybit_paths: list[str] = []

    def bybit_handler(request: httpx.Request) -> httpx.Response:
        bybit_paths.append(request.url.path)
        items = (
            []
            if request.url.path.endswith("/realtime")
            else [
                {
                    "orderId": "32",
                    "orderLinkId": "bh-order-p",
                    "symbol": "ORDERUSDT",
                    "side": "Sell",
                    "orderStatus": "Filled",
                    "price": "0.051",
                    "qty": "20",
                    "cumExecQty": "20",
                    "reduceOnly": False,
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": items, "nextPageCursor": ""},
            },
        )

    bybit_http = httpx.AsyncClient(
        transport=httpx.MockTransport(bybit_handler),
        base_url="https://bybit.test",
    )
    bybit = BybitAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        client=bybit_http,
    )

    lookup = await bybit.order_by_client_id(
        market="perp",
        symbol="ORDERUSDT",
        client_order_id="bh-order-p",
    )

    assert bybit_paths == ["/v5/order/realtime", "/v5/order/history"]
    assert lookup.order is not None
    assert lookup.order.exchange_order_id == "32"
    await bybit_http.aclose()

    def bitget_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spot/trade/orderInfo"):
            data: object = [
                {
                    "symbol": "ORDERUSDT",
                    "orderId": "42",
                    "clientOid": "bh-order-s",
                    "price": "0.05",
                    "size": "20",
                    "baseVolume": "4",
                    "side": "buy",
                    "status": "partially_filled",
                }
            ]
        else:
            data = {
                "symbol": "ORDERUSDT",
                "orderId": "43",
                "clientOid": "bh-order-p",
                "price": "0.051",
                "size": "20",
                "baseVolume": "20",
                "side": "sell",
                "state": "filled",
                "reduceOnly": "no",
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
    bitget._account_generation = "classic"

    spot_lookup = await bitget.order_by_client_id(
        market="spot",
        symbol="ORDERUSDT",
        client_order_id="bh-order-s",
    )
    perp_lookup = await bitget.order_by_client_id(
        market="perp",
        symbol="ORDERUSDT",
        client_order_id="bh-order-p",
    )

    assert spot_lookup.order is not None
    assert spot_lookup.order.filled_quantity == Decimal("4")
    assert perp_lookup.order is not None
    assert perp_lookup.order.status == "filled"
    await bitget_http.aclose()


async def test_gate_and_mexc_orders_are_found_by_client_id() -> None:
    def gate_handler(request: httpx.Request) -> httpx.Response:
        if "/spot/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "52",
                    "text": "t-bh-order-s",
                    "currency_pair": "ORDER_USDT",
                    "side": "buy",
                    "status": "open",
                    "price": "0.05",
                    "amount": "20",
                    "filled_amount": "0",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "53",
                "text": "t-bh-order-p",
                "contract": "ORDER_USDT",
                "size": -20,
                "left": -5,
                "price": "0.051",
                "status": "open",
                "is_reduce_only": True,
            },
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

    spot_lookup = await gate.order_by_client_id(
        market="spot",
        symbol="ORDER_USDT",
        client_order_id="t-bh-order-s",
    )
    perp_lookup = await gate.order_by_client_id(
        market="perp",
        symbol="ORDER_USDT",
        client_order_id="t-bh-order-p",
    )

    assert spot_lookup.order is not None
    assert spot_lookup.order.exchange_order_id == "52"
    assert perp_lookup.order is not None
    assert perp_lookup.order.filled_quantity == Decimal("15")
    assert perp_lookup.order.reduce_only is True
    await gate_http.aclose()

    missing_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                json={"label": "ORDER_NOT_FOUND"},
            )
        ),
        base_url="https://gate.test",
    )
    missing_gate = GateAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=missing_http,
    )
    missing = await missing_gate.order_by_client_id(
        market="perp",
        symbol="ORDER_USDT",
        client_order_id="t-bh-missing",
    )
    assert missing.order is None
    assert missing.complete is True
    await missing_http.aclose()

    def mexc_spot_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbol": "ORDERUSDT",
                "orderId": "62",
                "clientOrderId": "bh-order-s",
                "price": "0.05",
                "origQty": "20",
                "executedQty": "20",
                "status": "FILLED",
                "side": "BUY",
            },
        )

    def mexc_perp_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "orderId": "63",
                    "externalOid": "bh-order-p",
                    "symbol": "ORDER_USDT",
                    "price": "0.051",
                    "vol": "20",
                    "dealVol": "20",
                    "side": 3,
                    "state": 3,
                },
            },
        )

    mexc_spot = httpx.AsyncClient(
        transport=httpx.MockTransport(mexc_spot_handler),
        base_url="https://mexc-spot.test",
    )
    mexc_perp = httpx.AsyncClient(
        transport=httpx.MockTransport(mexc_perp_handler),
        base_url="https://mexc-perp.test",
    )
    mexc = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=mexc_spot,
        perp_client=mexc_perp,
    )

    spot_lookup = await mexc.order_by_client_id(
        market="spot",
        symbol="ORDERUSDT",
        client_order_id="bh-order-s",
    )
    perp_lookup = await mexc.order_by_client_id(
        market="perp",
        symbol="ORDER_USDT",
        client_order_id="bh-order-p",
    )

    assert spot_lookup.order is not None
    assert spot_lookup.order.exchange_order_id == "62"
    assert perp_lookup.order is not None
    assert perp_lookup.order.side == "sell"
    assert perp_lookup.order.filled_quantity == Decimal("20")
    await mexc_spot.aclose()
    await mexc_perp.aclose()
