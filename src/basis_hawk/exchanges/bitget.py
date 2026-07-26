from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from basis_hawk.exchanges.base import (
    ExchangeAdapter,
    PublicClient,
    as_list,
    decimal_increment,
)
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class BitgetAdapter(ExchangeAdapter):
    name = "bitget"
    product_type = "USDT-FUTURES"

    def __init__(self, *, timeout: float = 10) -> None:
        self.http = PublicClient(
            "https://api.bitget.com",
            timeout=timeout,
            minimum_interval=0.06,
        )

    @staticmethod
    def _data(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or payload.get("code") != "00000":
            raise RuntimeError(f"Bitget public API error: {payload!r}")
        return as_list(payload.get("data", []))

    async def instruments(self) -> list[InstrumentPair]:
        spot_payload, perp_payload = await asyncio.gather(
            self.http.get("/api/v2/spot/public/symbols"),
            self.http.get(
                "/api/v2/mix/market/contracts",
                productType=self.product_type,
            ),
        )
        spots = {
            str(item["baseCoin"]): item
            for item in self._data(spot_payload)
            if item.get("quoteCoin") == "USDT" and item.get("status") == "online"
        }
        perps = {
            str(item["baseCoin"]): item
            for item in self._data(perp_payload)
            if item.get("quoteCoin") == "USDT"
            and item.get("symbolStatus") == "normal"
            and item.get("symbolType") == "perpetual"
        }
        return [
            InstrumentPair(
                exchange=Exchange.BITGET,
                base_asset=base,
                spot_symbol=str(spots[base]["symbol"]),
                perp_symbol=str(perps[base]["symbol"]),
                funding_interval_hours=Decimal(str(perps[base].get("fundInterval") or 8)),
                spot_price_increment=decimal_increment(
                    spots[base].get("pricePrecision")
                ),
                spot_quantity_increment=decimal_increment(
                    spots[base].get("quantityPrecision")
                ),
                spot_min_quantity=Decimal(
                    str(spots[base].get("minTradeAmount") or "0")
                ),
                spot_min_notional=Decimal(
                    str(spots[base].get("minTradeUSDT") or "0")
                ),
                perp_price_increment=(
                    decimal_increment(perps[base].get("pricePlace"))
                    * Decimal(str(perps[base].get("priceEndStep") or "1"))
                ),
                perp_quantity_increment=Decimal(
                    str(perps[base].get("sizeMultiplier") or "0")
                ),
                perp_min_quantity=Decimal(
                    str(perps[base].get("minTradeNum") or "0")
                ),
                perp_min_notional=Decimal(
                    str(perps[base].get("minTradeUSDT") or "0")
                ),
                perp_contract_size=Decimal("1"),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def _tickers(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        spot_payload, perp_payload = await asyncio.gather(
            self.http.get("/api/v2/spot/market/tickers"),
            self.http.get(
                "/api/v2/mix/market/tickers",
                productType=self.product_type,
            ),
        )
        return self._data(spot_payload), self._data(perp_payload)

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        spot_items, perp_items = await self._tickers()
        spots = {str(item["symbol"]): item for item in spot_items}
        perps = {str(item["symbol"]): item for item in perp_items}
        now = datetime.now(UTC)
        results: list[MarketQuote] = []
        for pair in pairs:
            spot = spots.get(pair.spot_symbol)
            perp = perps.get(pair.perp_symbol)
            if not spot or not perp:
                continue
            try:
                spot_ask = Decimal(str(spot["askPr"]))
                perp_bid = Decimal(str(perp["bidPr"]))
                if spot_ask <= 0 or perp_bid <= 0:
                    continue
                timestamps = [
                    int(value)
                    for value in (spot.get("ts"), perp.get("ts"))
                    if value not in (None, "")
                ]
                observed_at = (
                    datetime.fromtimestamp(min(timestamps) / 1000, tz=UTC)
                    if timestamps
                    else now
                )
                results.append(
                    MarketQuote(
                        exchange=Exchange.BITGET,
                        base_asset=pair.base_asset,
                        observed_at=observed_at,
                        spot_bid=Decimal(str(spot["bidPr"])),
                        spot_bid_qty=Decimal(str(spot["bidSz"])),
                        spot_ask=spot_ask,
                        spot_ask_qty=Decimal(str(spot["askSz"])),
                        perp_bid=perp_bid,
                        perp_bid_qty=Decimal(str(perp["bidSz"])),
                        perp_ask=Decimal(str(perp["askPr"])),
                        perp_ask_qty=Decimal(str(perp["askSz"])),
                        spot_quote_volume_24h=Decimal(str(spot["quoteVolume"])),
                        perp_quote_volume_24h=Decimal(
                            str(perp.get("quoteVolume") or perp.get("usdtVolume") or "0")
                        ),
                    )
                )
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
        return results

    async def current_funding(
        self, pairs: list[InstrumentPair]
    ) -> list[FundingObservation]:
        payload = await self.http.get(
            "/api/v2/mix/market/current-fund-rate",
            productType=self.product_type,
        )
        by_symbol = {str(item["symbol"]): item for item in self._data(payload)}
        now = datetime.now(UTC)
        results: list[FundingObservation] = []
        for pair in pairs:
            item = by_symbol.get(pair.perp_symbol)
            if not item or item.get("fundingRate") in (None, ""):
                continue
            try:
                results.append(
                    FundingObservation(
                        exchange=Exchange.BITGET,
                        base_asset=pair.base_asset,
                        rate=Decimal(str(item["fundingRate"])),
                        funding_at=now,
                        observed_at=now,
                        next_funding_at=datetime.fromtimestamp(
                            int(item["nextUpdate"]) / 1000, tz=UTC
                        ),
                        interval_hours=Decimal(
                            str(item.get("fundingRateInterval") or pair.funding_interval_hours)
                        ),
                    )
                )
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
        return results

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        results: dict[datetime, FundingObservation] = {}
        for page_number in range(1, 6):
            payload = await self.http.get(
                "/api/v2/mix/market/history-fund-rate",
                symbol=pair.perp_symbol,
                productType=self.product_type,
                pageSize=100,
                pageNo=page_number,
            )
            items = self._data(payload)
            oldest: datetime | None = None
            for item in items:
                funding_at = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=UTC)
                oldest = funding_at if oldest is None else min(oldest, funding_at)
                if start <= funding_at <= end:
                    results[funding_at] = FundingObservation(
                        exchange=Exchange.BITGET,
                        base_asset=pair.base_asset,
                        rate=Decimal(str(item["fundingRate"])),
                        funding_at=funding_at,
                        observed_at=datetime.now(UTC),
                        settled=True,
                        interval_hours=pair.funding_interval_hours,
                    )
            if len(items) < 100 or (oldest is not None and oldest <= start):
                break
        return [results[key] for key in sorted(results)]

    async def close(self) -> None:
        await self.http.close()
