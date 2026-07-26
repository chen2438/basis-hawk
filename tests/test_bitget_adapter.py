from datetime import UTC, datetime, timedelta

import httpx

from basis_hawk.exchanges.base import PublicClient
from basis_hawk.exchanges.bitget import BitgetAdapter


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v2/spot/public/symbols":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "status": "online",
                    }
                ],
            },
        )
    if path == "/api/v2/mix/market/contracts":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "symbolStatus": "normal",
                        "symbolType": "perpetual",
                        "fundInterval": "4",
                    }
                ],
            },
        )
    if path == "/api/v2/spot/market/tickers":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "askPr": "100",
                        "askSz": "2",
                        "quoteVolume": "2000000",
                        "ts": "1785087000000",
                    }
                ],
            },
        )
    if path == "/api/v2/mix/market/tickers":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "bidPr": "101",
                        "bidSz": "3",
                        "quoteVolume": "3000000",
                        "ts": "1785087001000",
                    }
                ],
            },
        )
    if path == "/api/v2/mix/market/current-fund-rate":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingRateInterval": "4",
                        "nextUpdate": "1785110400000",
                    }
                ],
            },
        )
    if path == "/api/v2/mix/market/history-fund-rate":
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingTime": str(int(datetime.now(UTC).timestamp() * 1000)),
                    }
                ],
            },
        )
    return httpx.Response(404)


async def test_bitget_normalizes_public_responses() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.bitget.com",
    )
    adapter = BitgetAdapter.__new__(BitgetAdapter)
    adapter.http = PublicClient("", client=client, minimum_interval=0)
    pairs = await adapter.instruments()
    quotes = await adapter.quotes(pairs)
    current = await adapter.current_funding(pairs)
    history = await adapter.funding_history(
        pairs[0],
        start=datetime.now(UTC) - timedelta(days=1),
        end=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert pairs[0].base_asset == "BTC"
    assert str(pairs[0].funding_interval_hours) == "4"
    assert str(quotes[0].spot_ask) == "100"
    assert str(quotes[0].perp_quote_volume_24h) == "3000000"
    assert str(current[0].interval_hours) == "4"
    assert history[0].settled is True
    await client.aclose()
