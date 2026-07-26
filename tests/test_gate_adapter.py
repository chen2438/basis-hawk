from datetime import UTC, datetime, timedelta

import httpx

from basis_hawk.exchanges.base import PublicClient
from basis_hawk.exchanges.gate import GateAdapter


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/v4")
    if path == "/spot/currency_pairs":
        return httpx.Response(
            200,
            json=[
                {
                    "id": "BTC_USDT",
                    "base": "BTC",
                    "quote": "USDT",
                    "trade_status": "tradable",
                    "type": "normal",
                    "precision": 2,
                    "amount_precision": 6,
                    "min_base_amount": "0.0001",
                    "min_quote_amount": "5",
                }
            ],
        )
    if path == "/futures/usdt/contracts":
        return httpx.Response(
            200,
            json=[
                {
                    "name": "BTC_USDT",
                    "status": "trading",
                    "type": "direct",
                    "in_delisting": False,
                    "is_pre_market": False,
                    "quanto_multiplier": "0.001",
                    "funding_interval": 14400,
                    "funding_next_apply": 1785110400,
                    "order_price_round": "0.1",
                    "order_size_min": 1,
                }
            ],
        )
    if path == "/spot/tickers":
        return httpx.Response(
            200,
            json=[
                {
                    "currency_pair": "BTC_USDT",
                    "highest_bid": "99",
                    "highest_size": "3",
                    "lowest_ask": "100",
                    "lowest_size": "2",
                    "quote_volume": "2000000",
                }
            ],
        )
    if path == "/futures/usdt/tickers":
        return httpx.Response(
            200,
            json=[
                {
                    "contract": "BTC_USDT",
                    "highest_bid": "101",
                    "highest_size": "3000",
                    "lowest_ask": "102",
                    "lowest_size": "2000",
                    "quanto_multiplier": "0.001",
                    "volume_24h_quote": "3000000",
                    "funding_rate": "0.0001",
                }
            ],
        )
    if path == "/futures/usdt/funding_rate":
        return httpx.Response(
            200,
            json=[
                {
                    "t": int(datetime.now(UTC).timestamp()),
                    "r": "0.0001",
                }
            ],
        )
    return httpx.Response(404)


async def test_gate_normalizes_public_responses() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.gateio.ws/api/v4",
    )
    adapter = GateAdapter.__new__(GateAdapter)
    adapter.http = PublicClient("", client=client, minimum_interval=0)
    adapter._contracts = {}
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
    assert pairs[0].trading_rules_complete is True
    assert str(pairs[0].spot_price_increment) == "0.01"
    assert str(pairs[0].perp_base_quantity_increment) == "0.001"
    assert str(quotes[0].perp_bid_qty) == "3.000"
    assert str(quotes[0].perp_quote_volume_24h) == "3000000"
    assert str(current[0].interval_hours) == "4"
    assert history[0].settled is True
    await client.aclose()
