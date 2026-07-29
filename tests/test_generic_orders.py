from decimal import Decimal

import pytest
from pydantic import ValidationError

from basis_hawk.accounts import (
    BinanceAccountClient,
    BitgetAccountClient,
    BybitAccountClient,
    GateAccountClient,
    MexcAccountClient,
    OkxAccountClient,
    OrderMode,
    PerpMarginMode,
    PerpPositionSide,
    PositionMode,
    PrivateOrderRequest,
    UnsupportedTradingError,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)


def _order(**values: object) -> PrivateOrderRequest:
    defaults: dict[str, object] = {
        "market": "perp",
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": Decimal("1"),
        "mode": OrderMode.MAKER,
        "limit_price": Decimal("50000"),
        "client_order_id": "genericorder1",
        "position_mode": PositionMode.HEDGE,
        "position_side": PerpPositionSide.LONG,
    }
    defaults.update(values)
    return PrivateOrderRequest(**defaults)


def test_generic_order_model_supports_arbitrary_directions_and_modes() -> None:
    spot_sell = _order(
        market="spot",
        side="sell",
        position_mode=PositionMode.UNKNOWN,
        position_side=None,
    )
    assert spot_sell.side == "sell"
    market_close = _order(
        side="sell",
        mode=OrderMode.MARKET,
        limit_price=None,
        reduce_only=True,
    )
    assert market_close.position_side == PerpPositionSide.LONG

    with pytest.raises(ValidationError, match="require a limit price"):
        _order(limit_price=None)
    with pytest.raises(ValidationError, match="position side"):
        _order(position_side=None)
    with pytest.raises(ValidationError, match="long position action"):
        _order(side="sell")


async def test_binance_maps_maker_and_native_market_orders(monkeypatch) -> None:
    client = BinanceAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    requests: list[tuple[str, str, dict[str, object]]] = []

    async def request(_client, method, path, **params):
        requests.append((method, path, params))
        return {
            "symbol": params["symbol"],
            "clientOrderId": params["newClientOrderId"],
            "orderId": 1,
        }

    monkeypatch.setattr(client, "_signed_request", request)
    await client.place_order(
        _order(
            market="spot",
            side="sell",
            position_mode=PositionMode.UNKNOWN,
            position_side=None,
        )
    )
    await client.place_order(
        _order(
            mode=OrderMode.MARKET,
            limit_price=None,
            side="sell",
            reduce_only=True,
        )
    )

    assert requests[0][2]["type"] == "LIMIT_MAKER"
    assert "timeInForce" not in requests[0][2]
    assert requests[1][2]["type"] == "MARKET"
    assert requests[1][2]["positionSide"] == "LONG"
    assert "price" not in requests[1][2]
    await client.close()


async def test_okx_maps_post_only_cross_margin_and_long_side(monkeypatch) -> None:
    client = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    requests: list[tuple[str, dict[str, object]]] = []

    async def post(path, **values):
        requests.append((path, values))
        return {
            "code": "0",
            "data": [{"sCode": "0", "ordId": "1", "clOrdId": values["clOrdId"]}],
        }

    monkeypatch.setattr(client, "_post", post)
    await client.place_order(_order(margin_mode=PerpMarginMode.CROSS))

    assert requests[0][1]["ordType"] == "post_only"
    assert requests[0][1]["tdMode"] == "cross"
    assert requests[0][1]["posSide"] == "long"
    await client.close()


async def test_bybit_maps_market_close_to_hedged_long_position(monkeypatch) -> None:
    client = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    requests: list[tuple[str, dict[str, object]]] = []

    async def post(path, **values):
        requests.append((path, values))
        return {
            "retCode": 0,
            "result": {"orderId": "1", "orderLinkId": values["orderLinkId"]},
        }

    monkeypatch.setattr(client, "_post", post)
    await client.place_order(
        _order(
            side="sell",
            reduce_only=True,
            mode=OrderMode.MARKET,
            limit_price=None,
        )
    )

    assert requests[0][1]["orderType"] == "Market"
    assert requests[0][1]["positionIdx"] == 1
    assert requests[0][1]["reduceOnly"] is True
    assert "price" not in requests[0][1]
    await client.close()


async def test_bitget_classic_maps_post_only_long_open(monkeypatch) -> None:
    client = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    requests: list[tuple[str, dict[str, object]]] = []

    async def generation():
        return "classic"

    async def post(path, **values):
        requests.append((path, values))
        return {
            "code": "00000",
            "data": {"orderId": "1", "clientOid": values["clientOid"]},
        }

    monkeypatch.setattr(client, "_detect_account_generation", generation)
    monkeypatch.setattr(client, "_post", post)
    await client.place_order(_order())

    assert requests[0][1]["force"] == "post_only"
    assert requests[0][1]["side"] == "buy"
    assert requests[0][1]["tradeSide"] == "open"
    await client.close()


async def test_gate_maps_post_only_and_market_with_safe_spot_buy_guard(
    monkeypatch,
) -> None:
    client = GateAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    requests: list[tuple[str, dict[str, object]]] = []

    async def post(path, *, body):
        requests.append((path, body))
        return {"id": "1", "text": body["text"]}

    monkeypatch.setattr(client, "_post", post)
    await client.place_order(
        _order(
            market="spot",
            side="sell",
            position_mode=PositionMode.UNKNOWN,
            position_side=None,
            client_order_id="t-maker-1",
        )
    )
    await client.place_order(
        _order(
            side="sell",
            reduce_only=True,
            mode=OrderMode.MARKET,
            limit_price=None,
            client_order_id="t-market-1",
        )
    )

    assert requests[0][1]["time_in_force"] == "poc"
    assert requests[1][1]["price"] == "0"
    assert requests[1][1]["tif"] == "ioc"
    with pytest.raises(UnsupportedTradingError, match="quote quantity"):
        await client.place_order(
            _order(
                market="spot",
                mode=OrderMode.MARKET,
                limit_price=None,
                position_mode=PositionMode.UNKNOWN,
                position_side=None,
                client_order_id="t-market-buy",
            )
        )
    await client.close()


async def test_mexc_maps_maker_spot_and_cross_market_long_close(
    monkeypatch,
) -> None:
    client = MexcAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    spot_requests: list[dict[str, object]] = []
    perp_requests: list[dict[str, object]] = []

    async def spot_signed(method, path, **values):
        spot_requests.append(values)
        return {
            "orderId": "1",
            "clientOrderId": values["newClientOrderId"],
        }

    async def perp_post(path, body):
        perp_requests.append(body)
        return {"success": True, "code": 0, "data": "2"}

    monkeypatch.setattr(client, "_spot_signed", spot_signed)
    monkeypatch.setattr(client, "_perp_post", perp_post)
    client._configured_leverage["BTCUSDT"] = 3
    await client.place_order(
        _order(
            market="spot",
            side="sell",
            position_mode=PositionMode.UNKNOWN,
            position_side=None,
        )
    )
    await client.place_order(
        _order(
            side="sell",
            reduce_only=True,
            mode=OrderMode.MARKET,
            limit_price=None,
            margin_mode=PerpMarginMode.CROSS,
        )
    )

    assert spot_requests[0]["type"] == "LIMIT_MAKER"
    assert perp_requests[0]["side"] == 4
    assert perp_requests[0]["type"] == 5
    assert perp_requests[0]["openType"] == 2
    await client.close()


async def test_five_exchanges_normalize_live_adl_to_five_levels(
    monkeypatch,
) -> None:
    binance = BinanceAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    okx = OkxAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    bybit = BybitAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    bitget = BitgetAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    gate = GateAccountClient(SECRETS, ExchangeEnvironment.LIVE)

    async def binance_request(_client, method, path, **params):
        return [
            {
                "symbol": "BTCUSDT",
                "adlQuantile": {"LONG": 0, "SHORT": 4, "BOTH": 0},
            }
        ]

    async def okx_get(path, **params):
        return {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "posSide": "long", "adl": "2"}],
        }

    async def bybit_get(path, **params):
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "adlRankIndicator": 3,
                    }
                ]
            },
        }

    async def bitget_get(path, **params):
        return {
            "code": "00000",
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "holdSide": "short",
                    "adlRank": "0.2248",
                    "rank": "0.7752",
                }
            ],
        }

    async def gate_get(path, **params):
        return [{"contract": "BTC_USDT", "size": "-1", "adl_ranking": 1}]

    monkeypatch.setattr(binance, "_signed_request", binance_request)
    monkeypatch.setattr(okx, "_get", okx_get)
    monkeypatch.setattr(bybit, "_get", bybit_get)
    monkeypatch.setattr(bitget, "_get", bitget_get)
    monkeypatch.setattr(gate, "_get", gate_get)

    binance_adl = await binance.adl_ranks()
    assert [item.risk_level for item in binance_adl.positions] == [1, 5, 1]
    assert (await okx.adl_ranks()).positions[0].risk_level == 2
    assert (await bybit.adl_ranks()).positions[0].position_side == "short"
    assert (await bitget.adl_ranks()).positions[0].risk_level == 4
    assert (await gate.adl_ranks()).positions[0].risk_level == 5
    await binance.close()
    await okx.close()
    await bybit.close()
    await bitget.close()
    await gate.close()


async def test_mexc_reports_event_only_adl_capability() -> None:
    client = MexcAccountClient(SECRETS, ExchangeEnvironment.LIVE)
    result = await client.adl_ranks()
    assert result.complete is False
    assert result.event_only is True
    assert result.positions == []
    await client.close()
