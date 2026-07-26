import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from basis_hawk.accounts import (
    BinanceAccountClient,
    BitgetAccountClient,
    BybitAccountClient,
    LimitIocOrder,
    OkxAccountClient,
    PositionMode,
    PrivateRequestError,
    _hmac_base64,
    _hmac_hex,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
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
    assert close_short.exchange_order_id == "102"
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

    assert canceled.accepted is True
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


async def test_okx_places_spot_and_hedge_mode_perp_ioc_orders() -> None:
    requests: list[tuple[str, dict[str, object], httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        raw = json.loads(body)
        requests.append((request.url.path, raw, request.headers))
        timestamp = "2026-07-26T12:00:00.000Z"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["OK-ACCESS-KEY"] == SECRETS.api_key
        assert request.headers["OK-ACCESS-TIMESTAMP"] == timestamp
        assert request.headers["OK-ACCESS-SIGN"] == _hmac_base64(
            SECRETS.api_secret,
            f"{timestamp}POST{request.url.path}{body}",
        )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "sCode": "0",
                        "ordId": "301" if raw["instId"] == "ORDER-USDT" else "302",
                        "clOrdId": raw["clOrdId"],
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        clock=lambda: datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        client=http,
    )

    spot = await client.place_limit_ioc(
        LimitIocOrder(
            market="spot",
            symbol="ORDER-USDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.05"),
            client_order_id="bhopenspot",
        )
    )
    close_short = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDER-USDT-SWAP",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bhcloseperp",
            reduce_only=True,
            position_mode=PositionMode.HEDGE,
        )
    )

    assert spot.exchange_order_id == "301"
    assert close_short.exchange_order_id == "302"
    assert requests[0][0] == "/api/v5/trade/order"
    assert requests[0][1] == {
        "clOrdId": "bhopenspot",
        "instId": "ORDER-USDT",
        "ordType": "ioc",
        "px": "0.05",
        "side": "buy",
        "sz": "20",
        "tdMode": "cash",
    }
    assert requests[0][2]["x-simulated-trading"] == "1"
    assert requests[1][1]["tdMode"] == "isolated"
    assert requests[1][1]["posSide"] == "short"
    assert "reduceOnly" not in requests[1][1]
    await http.aclose()


async def test_okx_one_way_close_and_cancel_use_ack_receipts() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = json.loads(request.content.decode())
        requests.append((request.url.path, raw))
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "sCode": "0",
                        "ordId": raw.get("ordId", "401"),
                        "clOrdId": raw.get("clOrdId", "bhcloseoneway"),
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    order = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDER-USDT-SWAP",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bhcloseoneway",
            reduce_only=True,
            position_mode=PositionMode.ONE_WAY,
        )
    )
    canceled = await client.cancel_order(
        market="perp",
        symbol="ORDER-USDT-SWAP",
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.client_order_id,
    )

    assert requests[0][1]["reduceOnly"] is True
    assert "posSide" not in requests[0][1]
    assert requests[1] == (
        "/api/v5/trade/cancel-order",
        {"instId": "ORDER-USDT-SWAP", "ordId": "401"},
    )
    assert canceled.accepted is True
    assert canceled.exchange_order_id == "401"
    await http.aclose()


async def test_okx_configures_isolated_bounded_leverage_without_exposure() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = (
            json.loads(request.content.decode())
            if request.method == "POST"
            else dict(request.url.params)
        )
        requests.append((request.method, request.url.path, raw))
        if request.url.path.endswith("/leverage-info"):
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"posSide": "short", "lever": "1"}]},
            )
        if request.url.path.endswith("/orders-pending"):
            return httpx.Response(200, json={"code": "0", "data": []})
        if request.url.path.endswith("/positions"):
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"pos": "0"}]},
            )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "instId": "ORDER-USDT-SWAP",
                        "lever": "3",
                        "mgnMode": "isolated",
                        "posSide": "short",
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDER-USDT-SWAP",
        leverage=3,
        position_mode=PositionMode.HEDGE,
    )

    assert result.leverage == 3
    assert result.isolated is True
    post = next(item for item in requests if item[0] == "POST")
    assert post == (
        "POST",
        "/api/v5/account/set-leverage",
        {
            "instId": "ORDER-USDT-SWAP",
            "lever": "3",
            "mgnMode": "isolated",
            "posSide": "short",
        },
    )
    with pytest.raises(ValueError, match="between 1 and 10"):
        await client.configure_perp(
            symbol="ORDER-USDT-SWAP",
            leverage=11,
            position_mode=PositionMode.HEDGE,
        )
    await http.aclose()


async def test_okx_reuses_matching_leverage_and_refuses_exposure() -> None:
    pending_has_order = False
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/leverage-info"):
            leverage = "2" if pending_has_order else "3"
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"posSide": "net", "lever": leverage}]},
            )
        if request.url.path.endswith("/orders-pending"):
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"ordId": "1"}]},
            )
        return httpx.Response(200, json={"code": "0", "data": []})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDER-USDT-SWAP",
        leverage=3,
        position_mode=PositionMode.ONE_WAY,
    )
    assert result.leverage == 3
    assert requests == ["/api/v5/account/leverage-info"]

    pending_has_order = True
    with pytest.raises(PrivateRequestError, match="open orders or positions"):
        await client.configure_perp(
            symbol="ORDER-USDT-SWAP",
            leverage=3,
            position_mode=PositionMode.ONE_WAY,
        )
    await http.aclose()


async def test_okx_rejects_invalid_client_ids_and_failed_acknowledgements() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [{"sCode": "51000", "sMsg": "invalid order"}],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)
    invalid = LimitIocOrder(
        market="spot",
        symbol="ORDER-USDT",
        side="buy",
        quantity=Decimal("20"),
        limit_price=Decimal("0.05"),
        client_order_id="bh-open-spot",
    )
    with pytest.raises(ValueError, match="alphanumeric"):
        await client.place_limit_ioc(invalid)

    rejected = invalid.model_copy(update={"client_order_id": "bhopenspot"})
    with pytest.raises(PrivateRequestError, match="order submission"):
        await client.place_limit_ioc(rejected)
    await http.aclose()


async def test_bybit_places_spot_and_hedge_mode_perp_ioc_orders() -> None:
    requests: list[tuple[dict[str, object], httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        raw = json.loads(body)
        requests.append((raw, request.headers))
        timestamp = "1785088000000"
        assert request.headers["X-BAPI-SIGN-TYPE"] == "2"
        assert request.headers["X-BAPI-SIGN"] == _hmac_hex(
            SECRETS.api_secret,
            f"{timestamp}{SECRETS.api_key}5000{body}",
        )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "orderId": "501" if raw["category"] == "spot" else "502",
                    "orderLinkId": raw["orderLinkId"],
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        clock_ms=lambda: 1_785_088_000_000,
        client=http,
    )

    spot = await client.place_limit_ioc(
        LimitIocOrder(
            market="spot",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.05"),
            client_order_id="bh-open_spot",
        )
    )
    open_short = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="sell",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-open-perp",
            position_mode=PositionMode.HEDGE,
        )
    )

    assert spot.exchange_order_id == "501"
    assert open_short.exchange_order_id == "502"
    assert requests[0][0] == {
        "category": "spot",
        "isLeverage": 0,
        "orderLinkId": "bh-open_spot",
        "orderType": "Limit",
        "price": "0.05",
        "qty": "20",
        "side": "Buy",
        "symbol": "ORDERUSDT",
        "timeInForce": "IOC",
    }
    assert requests[1][0]["category"] == "linear"
    assert requests[1][0]["positionIdx"] == 2
    assert requests[1][0]["reduceOnly"] is False
    await http.aclose()


async def test_bybit_one_way_close_and_targeted_cancel_return_ack_receipts() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = json.loads(request.content.decode())
        requests.append((request.url.path, raw))
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "orderId": raw.get("orderId", "601"),
                    "orderLinkId": raw.get("orderLinkId", "bh-close-perp"),
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    order = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-close-perp",
            reduce_only=True,
            position_mode=PositionMode.ONE_WAY,
        )
    )
    canceled = await client.cancel_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.client_order_id,
    )

    assert requests[0][1]["positionIdx"] == 0
    assert requests[0][1]["reduceOnly"] is True
    assert requests[1] == (
        "/v5/order/cancel",
        {"category": "linear", "orderId": "601", "symbol": "ORDERUSDT"},
    )
    assert canceled.accepted is True
    assert canceled.exchange_order_id == "601"
    await http.aclose()


async def test_bybit_safely_switches_account_isolated_mode_and_sets_leverage() -> None:
    margin_mode = "REGULAR_MARGIN"
    current_leverage = "1"
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal margin_mode, current_leverage
        raw = (
            json.loads(request.content.decode())
            if request.method == "POST"
            else dict(request.url.params)
        )
        requests.append((request.method, request.url.path, raw))
        if request.url.path == "/v5/account/info":
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"marginMode": margin_mode}},
            )
        if request.url.path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": [], "nextPageCursor": ""},
                },
            )
        if request.url.path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "ORDERUSDT",
                                "positionIdx": 1,
                                "size": "0",
                                "leverage": current_leverage,
                            },
                            {
                                "symbol": "ORDERUSDT",
                                "positionIdx": 2,
                                "size": "0",
                                "leverage": current_leverage,
                            },
                        ],
                        "nextPageCursor": "",
                    },
                },
            )
        if request.url.path == "/v5/account/set-margin-mode":
            margin_mode = "ISOLATED_MARGIN"
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"reasons": []}},
            )
        current_leverage = str(raw["sellLeverage"])
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.HEDGE,
    )

    assert result.isolated is True
    assert result.leverage == 3
    margin_request = next(
        item for item in requests if item[1] == "/v5/account/set-margin-mode"
    )
    assert margin_request[2] == {"setMarginMode": "ISOLATED_MARGIN"}
    leverage_request = next(
        item for item in requests if item[1] == "/v5/position/set-leverage"
    )
    assert leverage_request[2] == {
        "buyLeverage": "3",
        "category": "linear",
        "sellLeverage": "3",
        "symbol": "ORDERUSDT",
    }
    with pytest.raises(ValueError, match="between 1 and 10"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=11,
            position_mode=PositionMode.HEDGE,
        )
    await http.aclose()


async def test_bybit_reuses_matching_configuration_and_refuses_exposure() -> None:
    current_leverage = "3"
    has_order = False
    mutations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            mutations.append(request.url.path)
        if request.url.path == "/v5/account/info":
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}},
            )
        if request.url.path == "/v5/order/realtime":
            orders = [{"orderId": "1"}] if has_order else []
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": orders, "nextPageCursor": ""},
                },
            )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "ORDERUSDT",
                            "positionIdx": 0,
                            "size": "0",
                            "leverage": current_leverage,
                        }
                    ],
                    "nextPageCursor": "",
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.ONE_WAY,
    )
    assert result.leverage == 3
    assert mutations == []

    current_leverage = "2"
    has_order = True
    with pytest.raises(PrivateRequestError, match="open orders or positions"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=3,
            position_mode=PositionMode.ONE_WAY,
        )
    assert mutations == []
    await http.aclose()


async def test_bybit_rejects_invalid_client_ids_and_missing_order_ack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)
    invalid = LimitIocOrder(
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        quantity=Decimal("20"),
        limit_price=Decimal("0.05"),
        client_order_id="invalid.client.id",
    )
    with pytest.raises(ValueError, match="ASCII"):
        await client.place_limit_ioc(invalid)

    valid = invalid.model_copy(update={"client_order_id": "bh-order"})
    with pytest.raises(PrivateRequestError, match="no order ID"):
        await client.place_limit_ioc(valid)
    await http.aclose()


async def test_bitget_places_spot_and_hedge_mode_perp_ioc_orders() -> None:
    requests: list[tuple[str, dict[str, object], httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        raw = json.loads(body)
        requests.append((request.url.path, raw, request.headers))
        assert request.headers["ACCESS-SIGN"] == _hmac_base64(
            SECRETS.api_secret,
            f"1785088000000POST{request.url.path}{body}",
        )
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": {
                    "orderId": str(701 + len(requests)),
                    "clientOid": raw["clientOid"],
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        clock_ms=lambda: 1_785_088_000_000,
        client=http,
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
    open_short = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="sell",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-open-perp",
            position_mode=PositionMode.HEDGE,
        )
    )
    close_short = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.052"),
            client_order_id="bh-close-perp",
            reduce_only=True,
            position_mode=PositionMode.HEDGE,
        )
    )

    assert spot.exchange_order_id == "702"
    assert open_short.exchange_order_id == "703"
    assert close_short.exchange_order_id == "704"
    assert requests[0][1] == {
        "clientOid": "bh-open-spot",
        "force": "ioc",
        "orderType": "limit",
        "price": "0.05",
        "side": "buy",
        "size": "20",
        "symbol": "ORDERUSDT",
    }
    assert requests[0][2]["paptrading"] == "1"
    assert requests[1][1]["marginMode"] == "isolated"
    assert requests[1][1]["side"] == "sell"
    assert requests[1][1]["tradeSide"] == "open"
    assert "reduceOnly" not in requests[1][1]
    assert requests[2][1]["side"] == "sell"
    assert requests[2][1]["tradeSide"] == "close"
    await http.aclose()


async def test_bitget_one_way_reduce_only_recovers_missing_order_id_and_cancels() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = json.loads(request.content.decode())
        requests.append((request.url.path, raw))
        if request.url.path.endswith("/place-order"):
            data = {"clientOid": raw["clientOid"]}
        else:
            data = {"orderId": "801", "clientOid": raw.get("clientOid", "")}
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    order = await client.place_limit_ioc(
        LimitIocOrder(
            market="perp",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.051"),
            client_order_id="bh-close-perp",
            reduce_only=True,
            position_mode=PositionMode.ONE_WAY,
        )
    )
    canceled = await client.cancel_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.client_order_id,
    )

    assert order.exchange_order_id is None
    assert requests[0][1]["side"] == "buy"
    assert requests[0][1]["reduceOnly"] == "YES"
    assert requests[1] == (
        "/api/v2/mix/order/cancel-order",
        {
            "clientOid": "bh-close-perp",
            "marginCoin": "USDT",
            "productType": "USDT-FUTURES",
            "symbol": "ORDERUSDT",
        },
    )
    assert canceled.accepted is True
    assert canceled.exchange_order_id == "801"
    await http.aclose()


async def test_bitget_safely_sets_isolated_margin_and_leverage() -> None:
    margin_mode = "crossed"
    current_leverage = "1"
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal margin_mode, current_leverage
        raw = (
            json.loads(request.content.decode())
            if request.method == "POST"
            else dict(request.url.params)
        )
        requests.append((request.method, request.url.path, raw))
        if request.url.path.endswith("/account/account"):
            data: object = {
                "marginMode": margin_mode,
                "posMode": "hedge_mode",
                "isolatedShortLever": current_leverage,
            }
        elif request.url.path.endswith("/orders-pending"):
            data = {"entrustedList": []}
        elif request.url.path.endswith("/all-position"):
            data = []
        elif request.url.path.endswith("/set-margin-mode"):
            margin_mode = "isolated"
            data = {
                "symbol": "ORDERUSDT",
                "marginMode": "isolated",
                "shortLeverage": current_leverage,
            }
        else:
            current_leverage = str(raw["leverage"])
            data = {
                "symbol": "ORDERUSDT",
                "marginMode": "isolated",
                "shortLeverage": current_leverage,
            }
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.HEDGE,
    )

    assert result.isolated is True
    assert result.leverage == 3
    margin_request = next(
        item for item in requests if item[1].endswith("/set-margin-mode")
    )
    assert margin_request[2]["marginMode"] == "isolated"
    leverage_request = next(
        item for item in requests if item[1].endswith("/set-leverage")
    )
    assert leverage_request[2] == {
        "leverage": "3",
        "marginCoin": "USDT",
        "productType": "USDT-FUTURES",
        "symbol": "ORDERUSDT",
    }
    with pytest.raises(ValueError, match="between 1 and 10"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=11,
            position_mode=PositionMode.HEDGE,
        )
    await http.aclose()


async def test_bitget_reuses_matching_configuration_and_refuses_exposure() -> None:
    current_leverage = "3"
    has_position = False
    mutations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            mutations.append(request.url.path)
        if request.url.path.endswith("/account/account"):
            data: object = {
                "marginMode": "isolated",
                "posMode": "one_way_mode",
                "isolatedShortLever": current_leverage,
            }
        elif request.url.path.endswith("/orders-pending"):
            data = {"entrustedList": []}
        else:
            data = (
                [{"symbol": "ORDERUSDT", "total": "20"}]
                if has_position
                else []
            )
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    result = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.ONE_WAY,
    )
    assert result.leverage == 3
    assert mutations == []

    current_leverage = "2"
    has_position = True
    with pytest.raises(PrivateRequestError, match="open orders or positions"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=3,
            position_mode=PositionMode.ONE_WAY,
        )
    assert mutations == []
    await http.aclose()


async def test_bitget_normalizes_hedge_close_and_rejects_bad_ack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": {
                        "symbol": "ORDERUSDT",
                        "orderId": "901",
                        "clientOid": "bh-close-perp",
                        "price": "0.051",
                        "size": "20",
                        "baseVolume": "0",
                        "side": "sell",
                        "tradeSide": "close",
                        "state": "live",
                    },
                },
            )
        return httpx.Response(200, json={"code": "00000", "data": None})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    lookup = await client.order_by_client_id(
        market="perp",
        symbol="ORDERUSDT",
        client_order_id="bh-close-perp",
    )
    assert lookup.order is not None
    assert lookup.order.side == "buy"
    assert lookup.order.reduce_only is True

    invalid = LimitIocOrder(
        market="spot",
        symbol="ORDERUSDT",
        side="buy",
        quantity=Decimal("20"),
        limit_price=Decimal("0.05"),
        client_order_id="invalid.client.id",
    )
    with pytest.raises(ValueError, match="supported ASCII"):
        await client.place_limit_ioc(invalid)
    valid = invalid.model_copy(update={"client_order_id": "bh-order"})
    with pytest.raises(PrivateRequestError, match="no result"):
        await client.place_limit_ioc(valid)
    await http.aclose()
