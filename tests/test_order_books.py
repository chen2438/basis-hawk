from __future__ import annotations

from decimal import Decimal

import pytest

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import Exchange
from basis_hawk.order_books import OrderBookUnavailable, RestOrderBookProvider


@pytest.mark.parametrize(
    ("exchange", "market", "payload", "url_marker"),
    [
        (
            Exchange.BINANCE,
            "spot",
            {
                "bids": [["100", "1"], ["99", "2"]],
                "asks": [["101", "1"], ["102", "2"]],
            },
            "/api/v3/depth",
        ),
        (
            Exchange.BINANCE,
            "perp",
            {
                "bids": [["100", "1"], ["99", "2"]],
                "asks": [["101", "1"], ["102", "2"]],
            },
            "/fapi/v1/depth",
        ),
        (
            Exchange.OKX,
            "perp",
            {
                "code": "0",
                "data": [
                    {
                        "bids": [["100", "1", "0", "1"], ["99", "2", "0", "1"]],
                        "asks": [["101", "1", "0", "1"], ["102", "2", "0", "1"]],
                    }
                ],
            },
            "/api/v5/market/books",
        ),
        (
            Exchange.MEXC,
            "spot",
            {
                "bids": [["100", "1"], ["99", "2"]],
                "asks": [["101", "1"], ["102", "2"]],
            },
            "/api/v3/depth",
        ),
        (
            Exchange.MEXC,
            "perp",
            {
                "success": True,
                "data": {
                    "bids": [["100", "1", "1"], ["99", "2", "1"]],
                    "asks": [["101", "1", "1"], ["102", "2", "1"]],
                },
            },
            "/api/v1/contract/depth/",
        ),
        (
            Exchange.BYBIT,
            "spot",
            {
                "retCode": 0,
                "result": {
                    "b": [["100", "1"], ["99", "2"]],
                    "a": [["101", "1"], ["102", "2"]],
                },
            },
            "/v5/market/orderbook",
        ),
        (
            Exchange.BITGET,
            "perp",
            {
                "code": "00000",
                "data": {
                    "bids": [["100", "1"], ["99", "2"]],
                    "asks": [["101", "1"], ["102", "2"]],
                },
            },
            "/api/v2/mix/market/orderbook",
        ),
        (
            Exchange.GATE,
            "spot",
            {
                "bids": [["100", "1"], ["99", "2"]],
                "asks": [["101", "1"], ["102", "2"]],
            },
            "/spot/order_book",
        ),
        (
            Exchange.GATE,
            "perp",
            {
                "bids": [{"p": "100", "s": 1}, {"p": "99", "s": 2}],
                "asks": [{"p": "101", "s": -1}, {"p": "102", "s": -2}],
            },
            "/futures/usdt/order_book",
        ),
    ],
)
async def test_rest_order_books_normalize_real_price_levels(
    exchange: Exchange,
    market: str,
    payload: object,
    url_marker: str,
) -> None:
    requests: list[tuple[str, dict[str, object], dict[str, str] | None]] = []

    async def request_json(url, params, headers):
        requests.append((url, params, headers))
        return payload

    provider = RestOrderBookProvider(request_json=request_json)
    result = await provider.fetch(
        exchange=exchange,
        environment=ExchangeEnvironment.LIVE,
        market=market,
        symbol="BTCUSDT",
        level=2,
    )

    assert result.maker_price(side="buy", level=2) == Decimal("99")
    assert result.maker_price(side="sell", level=2) == Decimal("102")
    assert url_marker in requests[0][0]


async def test_order_book_sandbox_routing_and_unsupported_mexc() -> None:
    requests: list[tuple[str, dict[str, object], dict[str, str] | None]] = []

    async def request_json(url, params, headers):
        requests.append((url, params, headers))
        return {
            "code": "0",
            "data": [
                {
                    "bids": [["100", "1"]],
                    "asks": [["101", "1"]],
                }
            ],
        }

    provider = RestOrderBookProvider(request_json=request_json)
    await provider.fetch(
        exchange=Exchange.OKX,
        environment=ExchangeEnvironment.SANDBOX,
        market="spot",
        symbol="BTC-USDT",
        level=1,
    )
    assert requests[0][2] == {"x-simulated-trading": "1"}

    with pytest.raises(OrderBookUnavailable, match="unsupported"):
        await provider.fetch(
            exchange=Exchange.MEXC,
            environment=ExchangeEnvironment.SANDBOX,
            market="spot",
            symbol="BTCUSDT",
            level=1,
        )


async def test_order_book_rejects_crossed_or_shallow_depth() -> None:
    async def crossed(url, params, headers):
        return {"bids": [["101", "1"]], "asks": [["100", "1"]]}

    provider = RestOrderBookProvider(request_json=crossed)
    with pytest.raises(OrderBookUnavailable, match="crossed"):
        await provider.fetch(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            market="spot",
            symbol="BTCUSDT",
            level=1,
        )

    async def shallow(url, params, headers):
        return {"bids": [["99", "1"]], "asks": [["100", "1"]]}

    provider = RestOrderBookProvider(request_json=shallow)
    with pytest.raises(OrderBookUnavailable, match="insufficient depth"):
        await provider.fetch(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            market="spot",
            symbol="BTCUSDT",
            level=2,
        )
