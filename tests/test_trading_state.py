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


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://private.test",
    )


async def test_binance_open_orders_and_positions_are_normalized() -> None:
    def spot_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "orderId": 1,
                    "clientOrderId": "bh-spot",
                    "symbol": "ORDERUSDT",
                    "side": "BUY",
                    "status": "NEW",
                    "price": "0.05",
                    "origQty": "100",
                    "executedQty": "25",
                }
            ],
        )

    def perp_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("positionRisk"):
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "ORDERUSDT",
                        "positionAmt": "-75",
                        "entryPrice": "0.051",
                        "markPrice": "0.05",
                        "liquidationPrice": "0.09",
                        "leverage": "2",
                        "marginType": "isolated",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "orderId": 2,
                    "clientOrderId": "bh-perp",
                    "symbol": "ORDERUSDT",
                    "side": "SELL",
                    "status": "NEW",
                    "price": "0.051",
                    "origQty": "100",
                    "executedQty": "75",
                    "reduceOnly": False,
                }
            ],
        )

    spot = _client(spot_handler)
    perp = _client(perp_handler)
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=spot,
        perp_client=perp,
    )
    state = await client.trading_state()
    assert state.complete is True
    assert [item.market for item in state.open_orders] == ["spot", "perp"]
    assert state.positions[0].side == "short"
    assert state.positions[0].quantity == 75
    assert state.positions[0].isolated is True
    await spot.aclose()
    await perp.aclose()


async def test_okx_and_bybit_trading_state_pagination() -> None:
    def okx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("positions"):
            data = [
                {
                    "instId": "ORDER-USDT-SWAP",
                    "pos": "-10",
                    "posSide": "net",
                    "avgPx": "0.05",
                    "markPx": "0.051",
                    "liqPx": "0.09",
                    "lever": "1",
                    "mgnMode": "isolated",
                }
            ]
        else:
            data = [
                {
                    "ordId": "1",
                    "clOrdId": "bh-okx",
                    "instType": "SPOT",
                    "instId": "ORDER-USDT",
                    "side": "buy",
                    "state": "live",
                    "px": "0.05",
                    "sz": "10",
                    "accFillSz": "2",
                    "reduceOnly": "false",
                }
            ]
        return httpx.Response(200, json={"code": "0", "data": data})

    okx_http = _client(okx_handler)
    okx = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=okx_http)
    okx_state = await okx.trading_state()
    assert okx_state.open_orders[0].client_order_id == "bh-okx"
    assert okx_state.positions[0].side == "short"
    await okx_http.aclose()

    seen_cursors: list[str | None] = []

    def bybit_handler(request: httpx.Request) -> httpx.Response:
        category = request.url.params["category"]
        cursor = request.url.params.get("cursor")
        if category == "spot":
            seen_cursors.append(cursor)
            result = (
                {
                    "list": [
                        {
                            "orderId": "1",
                            "orderLinkId": "bh-bybit",
                            "symbol": "ORDERUSDT",
                            "side": "Buy",
                            "orderStatus": "New",
                            "price": "0.05",
                            "qty": "10",
                            "cumExecQty": "1",
                        }
                    ],
                    "nextPageCursor": "next",
                }
                if cursor is None
                else {"list": [], "nextPageCursor": ""}
            )
        elif request.url.path.endswith("position/list"):
            result = {
                "list": [
                    {
                        "symbol": "ORDERUSDT",
                        "side": "Sell",
                        "size": "9",
                        "avgPrice": "0.051",
                        "markPrice": "0.05",
                        "liqPrice": "0.09",
                        "leverage": "1",
                        "tradeMode": 1,
                    }
                ],
                "nextPageCursor": "",
            }
        else:
            result = {"list": [], "nextPageCursor": ""}
        return httpx.Response(200, json={"retCode": 0, "result": result})

    bybit_http = _client(bybit_handler)
    bybit = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=bybit_http)
    bybit_state = await bybit.trading_state()
    assert seen_cursors == [None, "next"]
    assert bybit_state.open_orders[0].filled_quantity == 1
    assert bybit_state.positions[0].side == "short"
    await bybit_http.aclose()


async def test_bitget_gate_and_mexc_trading_states_are_normalized() -> None:
    def bitget_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("unfilled-orders"):
            data = [
                {
                    "orderId": "1",
                    "clientOid": "bh-bitget",
                    "symbol": "ORDERUSDT",
                    "side": "buy",
                    "status": "live",
                    "price": "0.05",
                    "size": "10",
                    "baseVolume": "2",
                }
            ]
        elif request.url.path.endswith("orders-pending"):
            data = {"entrustedList": []}
        else:
            data = [
                {
                    "symbol": "ORDERUSDT",
                    "holdSide": "short",
                    "total": "8",
                    "openPriceAvg": "0.051",
                    "markPrice": "0.05",
                    "liquidationPrice": "0.09",
                    "leverage": "1",
                    "marginMode": "isolated",
                }
            ]
        return httpx.Response(200, json={"code": "00000", "data": data})

    bitget_http = _client(bitget_handler)
    bitget = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=bitget_http,
    )
    bitget_state = await bitget.trading_state()
    assert bitget_state.open_orders[0].market == "spot"
    assert bitget_state.positions[0].quantity == 8
    await bitget_http.aclose()

    def gate_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("open_orders"):
            return httpx.Response(
                200,
                json=[
                    {
                        "currency_pair": "ORDER_USDT",
                        "total": 1,
                        "orders": [
                            {
                                "id": "1",
                                "text": "t-bh-gate",
                                "currency_pair": "ORDER_USDT",
                                "side": "buy",
                                "status": "open",
                                "price": "0.05",
                                "amount": "10",
                                "filled_amount": "3",
                            }
                        ],
                    }
                ],
            )
        if request.url.path.endswith("positions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "contract": "ORDER_USDT",
                        "size": "-7",
                        "entry_price": "0.051",
                        "mark_price": "0.05",
                        "liq_price": "0.09",
                        "leverage": "1",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    gate_http = _client(gate_handler)
    gate = GateAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=gate_http)
    gate_state = await gate.trading_state()
    assert gate_state.open_orders[0].filled_quantity == 3
    assert gate_state.positions[0].side == "short"
    await gate_http.aclose()

    def mexc_spot_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    def mexc_perp_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("open_positions"):
            data = [
                {
                    "symbol": "ORDER_USDT",
                    "positionType": 2,
                    "holdVol": "6",
                    "holdAvgPrice": "0.051",
                    "fairPrice": "0.05",
                    "liquidatePrice": "0.09",
                    "leverage": "1",
                    "openType": 1,
                }
            ]
        else:
            data = {
                "totalCount": 1,
                "resultList": [
                    {
                        "orderId": "1",
                        "externalOid": "bh-mexc",
                        "symbol": "ORDER_USDT",
                        "side": 3,
                        "state": 2,
                        "price": "0.051",
                        "vol": "10",
                        "dealVol": "4",
                    }
                ],
            }
        return httpx.Response(200, json={"success": True, "data": data})

    mexc_spot = _client(mexc_spot_handler)
    mexc_perp = _client(mexc_perp_handler)
    mexc = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=mexc_spot,
        perp_client=mexc_perp,
    )
    mexc_state = await mexc.trading_state()
    assert mexc_state.open_orders[0].side == "sell"
    assert mexc_state.positions[0].isolated is True
    await mexc_spot.aclose()
    await mexc_perp.aclose()
