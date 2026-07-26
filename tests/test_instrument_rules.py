import httpx

from basis_hawk.exchanges.base import PublicClient
from basis_hawk.exchanges.bybit import BybitAdapter
from basis_hawk.exchanges.mexc import MexcAdapter
from basis_hawk.exchanges.okx import OkxAdapter


async def test_okx_instrument_rules_preserve_contract_units() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["instType"] == "SPOT":
            data = [
                {
                    "baseCcy": "ORDER",
                    "quoteCcy": "USDT",
                    "instId": "ORDER-USDT",
                    "state": "live",
                    "tickSz": "0.00001",
                    "lotSz": "0.1",
                    "minSz": "1",
                }
            ]
        else:
            data = [
                {
                    "ctValCcy": "ORDER",
                    "settleCcy": "USDT",
                    "instId": "ORDER-USDT-SWAP",
                    "state": "live",
                    "ctType": "linear",
                    "alias": "",
                    "tickSz": "0.0001",
                    "lotSz": "1",
                    "minSz": "1",
                    "ctVal": "10",
                    "ctMult": "0.5",
                }
            ]
        return httpx.Response(200, json={"code": "0", "data": data})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    adapter = OkxAdapter.__new__(OkxAdapter)
    adapter.http = PublicClient("", client=client, minimum_interval=0)

    pairs = await adapter.instruments()

    assert len(pairs) == 1
    assert pairs[0].trading_rules_complete is True
    assert str(pairs[0].perp_quantity_increment) == "1"
    assert str(pairs[0].perp_contract_size) == "5.0"
    assert str(pairs[0].perp_base_quantity_increment) == "5.0"
    await client.aclose()


async def test_bybit_instrument_rules_use_spot_base_precision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        category = request.url.params["category"]
        if category == "spot":
            items = [
                {
                    "baseCoin": "ORDER",
                    "quoteCoin": "USDT",
                    "symbol": "ORDERUSDT",
                    "status": "Trading",
                    "priceFilter": {"tickSize": "0.00001"},
                    "lotSizeFilter": {
                        "basePrecision": "0.1",
                        "minOrderQty": "1",
                        "minOrderAmt": "5",
                    },
                }
            ]
        else:
            items = [
                {
                    "baseCoin": "ORDER",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "symbol": "ORDERUSDT",
                    "status": "Trading",
                    "contractType": "LinearPerpetual",
                    "isPreListing": False,
                    "fundingInterval": 480,
                    "priceFilter": {"tickSize": "0.0001"},
                    "lotSizeFilter": {
                        "qtyStep": "1",
                        "minOrderQty": "1",
                        "minNotionalValue": "5",
                    },
                }
            ]
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": items, "nextPageCursor": ""},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    adapter = BybitAdapter.__new__(BybitAdapter)
    adapter.http = PublicClient("", client=client, minimum_interval=0)

    pairs = await adapter.instruments()

    assert len(pairs) == 1
    assert pairs[0].trading_rules_complete is True
    assert str(pairs[0].spot_quantity_increment) == "0.1"
    assert str(pairs[0].perp_min_notional) == "5"
    await client.aclose()


async def test_mexc_instrument_rules_preserve_contract_size() -> None:
    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "baseAsset": "ORDER",
                            "quoteAsset": "USDT",
                            "symbol": "ORDERUSDT",
                            "status": "TRADING",
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "tickSize": "0.00001",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "1",
                                },
                                {
                                    "filterType": "MIN_NOTIONAL",
                                    "minNotional": "5",
                                },
                            ],
                        }
                    ]
                },
            )
        ),
        base_url="https://mexc-spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "baseCoin": "ORDER",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "symbol": "ORDER_USDT",
                            "state": 0,
                            "fundingRateCycle": 8,
                            "priceUnit": "0.0001",
                            "volUnit": "1",
                            "minVol": "1",
                            "contractSize": "10",
                        }
                    ],
                },
            )
        ),
        base_url="https://mexc-perp.test",
    )
    adapter = MexcAdapter.__new__(MexcAdapter)
    adapter.spot = PublicClient("", client=spot, minimum_interval=0)
    adapter.perp = PublicClient("", client=perp, minimum_interval=0)

    pairs = await adapter.instruments()

    assert len(pairs) == 1
    assert pairs[0].trading_rules_complete is True
    assert str(pairs[0].spot_min_notional) == "5"
    assert str(pairs[0].perp_contract_size) == "10"
    assert str(pairs[0].perp_base_quantity_increment) == "10"
    await spot.aclose()
    await perp.aclose()
