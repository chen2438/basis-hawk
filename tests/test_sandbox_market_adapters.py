from basis_hawk.models import Exchange
from basis_hawk.service import default_sandbox_adapters


async def test_sandbox_adapters_use_official_demo_and_testnet_routes() -> None:
    adapters = default_sandbox_adapters(timeout=1)
    try:
        binance = adapters[Exchange.BINANCE]
        assert str(binance.spot.client.base_url) == "https://demo-api.binance.com"
        assert str(binance.perp.client.base_url) == "https://demo-fapi.binance.com"

        okx = adapters[Exchange.OKX]
        assert okx.http.client.headers["x-simulated-trading"] == "1"

        bybit = adapters[Exchange.BYBIT]
        assert str(bybit.http.client.base_url) == "https://api-testnet.bybit.com"

        bitget = adapters[Exchange.BITGET]
        assert bitget.http.client.headers["paptrading"] == "1"

        gate = adapters[Exchange.GATE]
        assert str(gate.http.client.base_url).startswith("https://api-testnet.gateapi.io/")

        assert Exchange.MEXC not in adapters
    finally:
        for adapter in adapters.values():
            await adapter.close()
