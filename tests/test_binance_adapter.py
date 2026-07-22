from datetime import UTC, datetime, timedelta

import httpx

from basis_hawk.exchanges.base import PublicClient
from basis_hawk.exchanges.binance import BinanceAdapter


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v3/exchangeInfo":
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    }
                ]
            },
        )
    if path == "/fapi/v1/exchangeInfo":
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    }
                ]
            },
        )
    if path.endswith("bookTicker"):
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "askPrice": "100",
                    "askQty": "2",
                    "bidPrice": "101",
                    "bidQty": "3",
                }
            ],
        )
    if path.endswith("ticker/24hr"):
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "quoteVolume": "2000000"}])
    if path == "/fapi/v1/premiumIndex":
        now = int(datetime.now(UTC).timestamp() * 1000)
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "lastFundingRate": "0.0001",
                    "time": now,
                    "nextFundingTime": now + 28_800_000,
                }
            ],
        )
    if path == "/fapi/v1/fundingInfo":
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingIntervalHours": 4}])
    if path == "/fapi/v1/fundingRate":
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "fundingTime": int(datetime.now(UTC).timestamp() * 1000),
                }
            ],
        )
    return httpx.Response(404)


async def test_binance_normalizes_public_responses() -> None:
    transport = httpx.MockTransport(handler)
    spot_http = httpx.AsyncClient(transport=transport, base_url="https://api.binance.com")
    perp_http = httpx.AsyncClient(transport=transport, base_url="https://fapi.binance.com")
    adapter = BinanceAdapter.__new__(BinanceAdapter)
    adapter.spot = PublicClient("", client=spot_http, minimum_interval=0)
    adapter.perp = PublicClient("", client=perp_http, minimum_interval=0)
    pairs = await adapter.instruments()
    quotes = await adapter.quotes(pairs)
    current = await adapter.current_funding(pairs)
    history = await adapter.funding_history(
        pairs[0], start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC)
    )
    assert pairs[0].base_asset == "BTC"
    assert str(quotes[0].spot_ask) == "100"
    assert str(current[0].interval_hours) == "4"
    assert history[0].settled is True
    await spot_http.aclose()
    await perp_http.aclose()
