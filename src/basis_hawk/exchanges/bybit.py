from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.exchanges.base import ExchangeAdapter, PublicClient
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class BybitAdapter(ExchangeAdapter):
    name = "bybit"

    def __init__(self, *, timeout: float = 10) -> None:
        self.http = PublicClient("https://api.bybit.com", timeout=timeout, minimum_interval=0.06)

    async def _instruments(self, category: str) -> list[dict[str, object]]:
        cursor = ""
        results: list[dict[str, object]] = []
        while True:
            params: dict[str, object] = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = await self.http.get("/v5/market/instruments-info", **params)
            result = payload["result"]
            results.extend(result.get("list", []))
            cursor = result.get("nextPageCursor", "")
            if not cursor or category == "spot":
                return results

    async def instruments(self) -> list[InstrumentPair]:
        spot, linear = await asyncio.gather(self._instruments("spot"), self._instruments("linear"))
        spots = {
            str(item["baseCoin"]): item
            for item in spot
            if item.get("status") == "Trading" and item.get("quoteCoin") == "USDT"
        }
        perps = {
            str(item["baseCoin"]): item
            for item in linear
            if item.get("status") == "Trading"
            and item.get("quoteCoin") == "USDT"
            and item.get("settleCoin") == "USDT"
            and item.get("contractType") == "LinearPerpetual"
            and not item.get("isPreListing", False)
        }
        return [
            InstrumentPair(
                exchange=Exchange.BYBIT,
                base_asset=base,
                spot_symbol=str(spots[base]["symbol"]),
                perp_symbol=str(perps[base]["symbol"]),
                funding_interval_hours=Decimal(str(perps[base].get("fundingInterval", 480)))
                / Decimal("60"),
                spot_price_increment=Decimal(
                    str((spots[base].get("priceFilter") or {}).get("tickSize") or "0")
                ),
                spot_quantity_increment=Decimal(
                    str(
                        (spots[base].get("lotSizeFilter") or {}).get(
                            "basePrecision"
                        )
                        or "0"
                    )
                ),
                spot_min_quantity=Decimal(
                    str(
                        (spots[base].get("lotSizeFilter") or {}).get(
                            "minOrderQty"
                        )
                        or "0"
                    )
                ),
                spot_min_notional=Decimal(
                    str(
                        (spots[base].get("lotSizeFilter") or {}).get(
                            "minOrderAmt"
                        )
                        or "0"
                    )
                ),
                perp_price_increment=Decimal(
                    str((perps[base].get("priceFilter") or {}).get("tickSize") or "0")
                ),
                perp_quantity_increment=Decimal(
                    str(
                        (perps[base].get("lotSizeFilter") or {}).get("qtyStep")
                        or "0"
                    )
                ),
                perp_min_quantity=Decimal(
                    str(
                        (perps[base].get("lotSizeFilter") or {}).get(
                            "minOrderQty"
                        )
                        or "0"
                    )
                ),
                perp_min_notional=Decimal(
                    str(
                        (perps[base].get("lotSizeFilter") or {}).get(
                            "minNotionalValue"
                        )
                        or "0"
                    )
                ),
                perp_contract_size=Decimal("1"),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def _tickers(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        spot, linear = await asyncio.gather(
            self.http.get("/v5/market/tickers", category="spot"),
            self.http.get("/v5/market/tickers", category="linear"),
        )
        return spot["result"]["list"], linear["result"]["list"]

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        spot_items, perp_items = await self._tickers()
        spots = {item["symbol"]: item for item in spot_items}
        perps = {item["symbol"]: item for item in perp_items}
        now = datetime.now(UTC)
        return [
            MarketQuote(
                exchange=Exchange.BYBIT,
                base_asset=pair.base_asset,
                observed_at=now,
                spot_bid=Decimal(spots[pair.spot_symbol]["bid1Price"]),
                spot_bid_qty=Decimal(spots[pair.spot_symbol]["bid1Size"]),
                spot_ask=Decimal(spots[pair.spot_symbol]["ask1Price"]),
                spot_ask_qty=Decimal(spots[pair.spot_symbol]["ask1Size"]),
                perp_bid=Decimal(perps[pair.perp_symbol]["bid1Price"]),
                perp_bid_qty=Decimal(perps[pair.perp_symbol]["bid1Size"]),
                perp_ask=Decimal(perps[pair.perp_symbol]["ask1Price"]),
                perp_ask_qty=Decimal(perps[pair.perp_symbol]["ask1Size"]),
                spot_quote_volume_24h=Decimal(spots[pair.spot_symbol]["turnover24h"]),
                perp_quote_volume_24h=Decimal(perps[pair.perp_symbol]["turnover24h"]),
            )
            for pair in pairs
            if pair.spot_symbol in spots and pair.perp_symbol in perps
        ]

    async def current_funding(self, pairs: list[InstrumentPair]) -> list[FundingObservation]:
        _, perp_items = await self._tickers()
        perps = {item["symbol"]: item for item in perp_items}
        now = datetime.now(UTC)
        return [
            FundingObservation(
                exchange=Exchange.BYBIT,
                base_asset=pair.base_asset,
                rate=Decimal(perps[pair.perp_symbol]["fundingRate"]),
                funding_at=now,
                observed_at=now,
                next_funding_at=datetime.fromtimestamp(
                    int(perps[pair.perp_symbol]["nextFundingTime"]) / 1000, tz=UTC
                ),
                interval_hours=Decimal(
                    perps[pair.perp_symbol].get("fundingIntervalHour")
                    or pair.funding_interval_hours
                ),
            )
            for pair in pairs
            if pair.perp_symbol in perps
            and perps[pair.perp_symbol].get("fundingRate") not in (None, "")
        ]

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        payload = await self.http.get(
            "/v5/market/funding/history",
            category="linear",
            symbol=pair.perp_symbol,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=200,
        )
        return [
            FundingObservation(
                exchange=Exchange.BYBIT,
                base_asset=pair.base_asset,
                rate=Decimal(item["fundingRate"]),
                funding_at=datetime.fromtimestamp(int(item["fundingRateTimestamp"]) / 1000, tz=UTC),
                observed_at=datetime.now(UTC),
                settled=True,
                interval_hours=pair.funding_interval_hours,
            )
            for item in payload["result"]["list"]
        ]

    async def close(self) -> None:
        await self.http.close()
