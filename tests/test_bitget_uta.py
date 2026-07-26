import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from basis_hawk.accounts import (
    BitgetAccountClient,
    LimitIocOrder,
    PositionMode,
    PrivateRequestError,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)


def _settings(
    *,
    margin_mode: str = "crossed",
    leverage: str = "1",
    hold_mode: str = "hedge_mode",
) -> dict[str, object]:
    return {
        "accountMode": "unified",
        "accountLevel": "advanced",
        "assetMode": "multi_assets",
        "holdMode": hold_mode,
        "symbolConfigList": [
            {
                "category": "USDT-FUTURES",
                "symbol": "ORDERUSDT",
                "marginMode": margin_mode,
                "leverage": leverage,
            }
        ],
    }


async def test_bitget_uta_snapshot_detects_and_caches_account_generation() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/settings"):
            data: object = _settings(hold_mode="one_way_mode")
        elif request.url.path.endswith("/info"):
            data = {
                "permType": "read-and-write",
                "permissions": ["uta_trade", "uta_mgt"],
            }
        else:
            data = {
                "usdtEquity": "18",
                "assets": [
                    {
                        "coin": "USDT",
                        "available": "17",
                        "equity": "18",
                    }
                ],
            }
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    first = await client.snapshot()
    second = await client.snapshot()

    assert first.spot_usdt_available == Decimal("17")
    assert first.perp_usdt_available == Decimal("17")
    assert first.perp_usdt_equity == Decimal("18")
    assert first.shared_balance is True
    assert first.account_mode == "uta:unified:advanced:multi_assets"
    assert first.position_mode == PositionMode.ONE_WAY
    assert first.trade_permission is True
    assert second == first.model_copy(update={"observed_at": second.observed_at})
    assert paths.count("/api/v3/account/settings") == 3
    assert not any(path.startswith("/api/v2/") for path in paths)
    await http.aclose()


async def test_bitget_refuses_writes_when_account_generation_is_ambiguous() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={"code": "00000", "data": {"unexpected": True}},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    with pytest.raises(PrivateRequestError, match="identified safely"):
        await client.place_limit_ioc(
            LimitIocOrder(
                market="spot",
                symbol="ORDERUSDT",
                side="buy",
                quantity=Decimal("20"),
                limit_price=Decimal("0.05"),
                client_order_id="bh-ambiguous",
            )
        )

    assert methods == ["GET", "GET"]
    await http.aclose()


async def test_bitget_uta_places_and_cancels_paired_ioc_orders() -> None:
    writes: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"code": "00000", "data": _settings()},
            )
        body = json.loads(request.content.decode())
        writes.append((request.url.path, body))
        data = {
            "clientOid": body.get("clientOid", "bh-close-perp"),
            "orderId": str(900 + len(writes)),
        }
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        client=http,
    )

    spot = await client.place_limit_ioc(
        LimitIocOrder(
            market="spot",
            symbol="ORDERUSDT",
            side="buy",
            quantity=Decimal("20"),
            limit_price=Decimal("0.05"),
            client_order_id="bh.open:spot",
        )
    )
    await client.place_limit_ioc(
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
    close_order = await client.place_limit_ioc(
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
    canceled = await client.cancel_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id=close_order.exchange_order_id,
        client_order_id=close_order.client_order_id,
    )

    assert spot.exchange_order_id == "901"
    assert writes[0] == (
        "/api/v3/trade/place-order",
        {
            "category": "SPOT",
            "clientOid": "bh.open:spot",
            "orderType": "limit",
            "price": "0.05",
            "qty": "20",
            "side": "buy",
            "symbol": "ORDERUSDT",
            "timeInForce": "ioc",
        },
    )
    assert writes[1][1]["category"] == "USDT-FUTURES"
    assert writes[1][1]["marginMode"] == "isolated"
    assert writes[1][1]["posSide"] == "short"
    assert writes[2][1]["side"] == "buy"
    assert writes[2][1]["posSide"] == "short"
    assert "reduceOnly" not in writes[2][1]
    assert writes[3][0] == "/api/v3/trade/cancel-order"
    assert writes[3][1]["orderId"] == "903"
    assert canceled.accepted is True
    await http.aclose()


async def test_bitget_uta_reconciles_orders_fills_and_positions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/settings"):
            data: object = _settings()
        elif path.endswith("/unfilled-orders"):
            category = request.url.params["category"]
            item = {
                "orderId": "51",
                "clientOid": "bh-order",
                "category": category,
                "symbol": "ORDERUSDT",
                "price": "0.05",
                "qty": "20",
                "cumExecQty": "4",
                "orderStatus": "partially_filled",
                "side": "buy" if category == "SPOT" else "sell",
            }
            data = {
                "list": [item] * (100 if category == "SPOT" else 1),
                "cursor": "more" if category == "SPOT" else "",
            }
        elif path.endswith("/current-position"):
            data = {
                "list": [
                    {
                        "symbol": "ORDERUSDT",
                        "posSide": "short",
                        "total": "16",
                        "avgPrice": "0.051",
                        "markPrice": "0.05",
                        "liquidationPrice": "0.09",
                        "leverage": "2",
                        "marginMode": "isolated",
                    }
                ]
            }
        elif path.endswith("/fills"):
            data = {
                "list": [
                    {
                        "execId": "61",
                        "orderId": "51",
                        "clientOid": "bh-order",
                        "symbol": "ORDERUSDT",
                        "side": "sell",
                        "execQty": "4",
                        "execPrice": "0.051",
                        "feeDetail": [{"feeCoin": "USDT", "fee": "0.002"}],
                        "tradeScope": "taker",
                        "createdTime": "1785087000000",
                    }
                ],
                "cursor": "",
            }
        else:
            data = {
                "orderId": "51",
                "clientOid": "bh-order",
                "category": "USDT-FUTURES",
                "symbol": "ORDERUSDT",
                "price": "0.051",
                "qty": "20",
                "cumExecQty": "4",
                "orderStatus": "partially_filled",
                "side": "buy",
                "posSide": "short",
            }
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_785_088_000_000,
        client=http,
    )

    state = await client.trading_state()
    fills = await client.fills_for_order(
        market="perp",
        symbol="ORDERUSDT",
        exchange_order_id="51",
        client_order_id="bh-order",
        since=datetime(2026, 7, 26, tzinfo=UTC),
    )
    lookup = await client.order_by_client_id(
        market="perp",
        symbol="ORDERUSDT",
        client_order_id="bh-order",
    )

    assert state.complete is False
    assert len(state.open_orders) == 101
    assert state.positions[0].side == "short"
    assert state.positions[0].isolated is True
    assert fills.complete is True
    assert fills.fills[0].exchange_trade_id == "61"
    assert fills.fills[0].fee_amount == Decimal("0.002")
    assert lookup.order is not None
    assert lookup.order.side == "buy"
    assert lookup.order.reduce_only is True
    await http.aclose()


async def test_bitget_uta_confirms_isolated_short_leverage_without_exposure() -> None:
    margin_mode = "crossed"
    current_leverage = "1"
    has_position = False
    writes: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal margin_mode, current_leverage
        path = request.url.path
        if path.endswith("/settings"):
            data: object = _settings(
                margin_mode=margin_mode,
                leverage=current_leverage,
            )
        elif path.endswith("/unfilled-orders"):
            data = {"list": [], "cursor": ""}
        elif path.endswith("/current-position"):
            data = {
                "list": (
                    [{"symbol": "ORDERUSDT", "total": "1"}]
                    if has_position
                    else []
                )
            }
        else:
            body = json.loads(request.content.decode())
            writes.append(body)
            margin_mode = "isolated"
            current_leverage = str(body["leverage"])
            data = "success"
        return httpx.Response(200, json={"code": "00000", "data": data})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE, client=http)

    configured = await client.configure_perp(
        symbol="ORDERUSDT",
        leverage=3,
        position_mode=PositionMode.HEDGE,
    )

    assert configured.leverage == 3
    assert writes == [
        {
            "category": "USDT-FUTURES",
            "leverage": "3",
            "marginMode": "isolated",
            "posSide": "short",
            "symbol": "ORDERUSDT",
        }
    ]

    current_leverage = "2"
    client._uta_settings = _settings(
        margin_mode="isolated",
        leverage=current_leverage,
    )
    has_position = True
    with pytest.raises(PrivateRequestError, match="open orders or positions"):
        await client.configure_perp(
            symbol="ORDERUSDT",
            leverage=3,
            position_mode=PositionMode.HEDGE,
        )
    assert len(writes) == 1
    await http.aclose()
