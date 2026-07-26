from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from basis_hawk.accounts import (
    BinanceAccountClient,
    LimitIocOrder,
    PositionMode,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
)


async def test_binance_places_spot_and_hedge_mode_perp_ioc_orders() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raw = {
                key: values[0]
                for key, values in parse_qs(request.content.decode()).items()
            }
        else:
            raw = dict(request.url.params)
        requests.append((request.method, request.url.path, raw))
        side = raw.get("side", "BUY")
        market = "perp" if request.url.path.startswith("/fapi") else "spot"
        return httpx.Response(
            200,
            json={
                "symbol": raw.get("symbol", "ORDERUSDT"),
                "orderId": "101" if market == "spot" else "102",
                "clientOrderId": raw.get("newClientOrderId", "bh-cancel"),
                "price": raw.get("price", "0.05"),
                "origQty": raw.get("quantity", "20"),
                "executedQty": "0",
                "status": "EXPIRED",
                "side": side,
                "positionSide": raw.get("positionSide", "BOTH"),
                "reduceOnly": raw.get("reduceOnly") == "true",
            },
        )

    spot_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://spot.test",
    )
    perp_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=spot_http,
        perp_client=perp_http,
    )

    spot = await client.place_limit_ioc(
        LimitIocOrder(
            market="spot",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.05"),
            client_order_id="bh-open-spot",
        )
    )
    close_short = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-close-perp",
            reduce_only=True,
            position_mode=PositionMode.HEDGE,
        )
    )

    assert spot.exchange_order_id == "101"
    assert spot.reduce_only is False
    assert close_short.exchange_order_id == "102"
    assert close_short.reduce_only is True
    spot_request = requests[0][2]
    assert requests[0][:2] == ("POST", "/api/v3/order")
    assert spot_request["timeInForce"] == "IOC"
    assert spot_request["newOrderRespType"] == "RESULT"
    assert spot_request["quantity"] == "20"
    assert "signature" in spot_request
    perp_request = requests[1][2]
    assert requests[1][:2] == ("POST", "/fapi/v1/order")
    assert perp_request["positionSide"] == "SHORT"
    assert "reduceOnly" not in perp_request
    assert "signature" in perp_request
    await spot_http.aclose()
    await perp_http.aclose()


async def test_binance_one_way_open_and_cancel_use_signed_parameters() -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            params = {
                key: values[0]
                for key, values in parse_qs(request.content.decode()).items()
            }
        else:
            params = dict(request.url.params)
        requests.append((request.method, params))
        return httpx.Response(
            200,
            json={
                "symbol": "ORDERUSDT",
                "orderId": "201",
                "clientOrderId": params.get("newClientOrderId", "bh-open-perp"),
                "price": params.get("price", "0.051"),
                "origQty": params.get("quantity", "20"),
                "executedQty": "0",
                "status": "CANCELED" if request.method == "DELETE" else "NEW",
                "side": params.get("side", "SELL"),
                "positionSide": "BOTH",
                "reduceOnly": params.get("reduceOnly") == "true",
            },
        )

    spot_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://spot.test",
    )
    perp_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=spot_http,
        perp_client=perp_http,
    )

    order = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="sell",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-open-perp",
            position_mode=PositionMode.ONE_WAY,
        )
    )
    canceled = await client.cancel_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.client_order_id,
    )

    assert order.reduce_only is False
    assert canceled.status == "CANCELED"
    assert requests[0][1]["positionSide"] == "BOTH"
    assert requests[0][1]["reduceOnly"] == "false"
    assert requests[1][0] == "DELETE"
    assert requests[1][1]["orderId"] == "201"
    assert "origClientOrderId" not in requests[1][1]
    assert "signature" in requests[1][1]
    await spot_http.aclose()
    await perp_http.aclose()


async def test_binance_configures_isolated_margin_and_bounded_leverage() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = (
            {
                key: values[0]
                for key, values in parse_qs(request.content.decode()).items()
            }
            if request.method == "POST"
            else dict(request.url.params)
        )
        requests.append((request.method, request.url.path, params))
        if request.url.path.endswith("/openOrders"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/positionRisk"):
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "ORDERUSDT",
                        "positionAmt": "0",
                        "marginType": "cross",
                    }
                ],
            )
        if request.url.path.endswith("/marginType"):
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        return httpx.Response(
            200,
            json={
                "symbol": "ORDERUSDT",
                "leverage": int(params["leverage"]),
                "maxNotionalValue": "100000",
            },
        )

    spot_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://spot.test",
    )
    perp_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1785088000000,
        spot_client=spot_http,
        perp_client=perp_http,
    )

    result = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.ONE_WAY,
    )

    assert result.isolated is True
    assert result.leverage == 3
    assert [item[1] for item in requests] == [
        "/fapi/v1/openOrders",
        "/fapi/v3/positionRisk",
        "/fapi/v1/marginType",
        "/fapi/v1/leverage",
    ]
    assert requests[2][2]["marginType"] == "ISOLATED"
    assert "signature" in requests[2][2]
    with pytest.raises(ValueError, match="between 1 and 10"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=11,
            position_mode=PositionMode.ONE_WAY,
        )
    await spot_http.aclose()
    await perp_http.aclose()


async def test_binance_refuses_margin_mode_change_with_existing_exposure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openOrders"):
            return httpx.Response(200, json=[{"orderId": "1"}])
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "ORDERUSDT",
                    "positionAmt": "-20",
                    "marginType": "cross",
                }
            ],
        )

    spot_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://spot.test",
    )
    perp_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        spot_client=spot_http,
        perp_client=perp_http,
    )

    with pytest.raises(RuntimeError, match="open orders or positions"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=1,
            position_mode=PositionMode.ONE_WAY,
        )
    await spot_http.aclose()
    await perp_http.aclose()


def test_limit_ioc_order_rejects_unsafe_perpetual_directions() -> None:
    with pytest.raises(ValidationError, match="position mode"):
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="sell",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-open-perp",
        )
    with pytest.raises(ValidationError, match="must be reduce-only"):
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-close-perp",
            position_mode=PositionMode.ONE_WAY,
        )
