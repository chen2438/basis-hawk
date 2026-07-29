from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.exchanges.base import ExchangeAdapter, PublicClient
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class OkxAdapter(ExchangeAdapter):
    name = "okx"

    def __init__(
        self,
        *,
        timeout: float = 10,
        environment: ExchangeEnvironment = ExchangeEnvironment.LIVE,
    ) -> None:
        headers = (
            {"x-simulated-trading": "1"}
            if environment == ExchangeEnvironment.SANDBOX
            else None
        )
        self.http = PublicClient(
            "https://www.okx.com",
            timeout=timeout,
            minimum_interval=0.11,
            headers=headers,
        )

    async def instruments(self) -> list[InstrumentPair]:
        spot, swaps = await asyncio.gather(
            self.http.get("/api/v5/public/instruments", instType="SPOT"),
            self.http.get("/api/v5/public/instruments", instType="SWAP"),
        )
        spots = {
            item["baseCcy"]: item
            for item in spot.get("data", [])
            if item.get("state") == "live" and item.get("quoteCcy") == "USDT"
        }
        perps = {
            item["ctValCcy"]: item
            for item in swaps.get("data", [])
            if item.get("state") == "live"
            and item.get("settleCcy") == "USDT"
            and item.get("ctType") == "linear"
            and item.get("ctValCcy")
            and item.get("alias", "") == ""
        }
        return [
            InstrumentPair(
                exchange=Exchange.OKX,
                base_asset=base,
                spot_symbol=spots[base]["instId"],
                perp_symbol=perps[base]["instId"],
                spot_price_increment=Decimal(
                    str(spots[base].get("tickSz") or "0")
                ),
                spot_quantity_increment=Decimal(
                    str(spots[base].get("lotSz") or "0")
                ),
                spot_min_quantity=Decimal(
                    str(spots[base].get("minSz") or "0")
                ),
                perp_price_increment=Decimal(
                    str(perps[base].get("tickSz") or "0")
                ),
                perp_quantity_increment=Decimal(
                    str(perps[base].get("lotSz") or "0")
                ),
                perp_min_quantity=Decimal(
                    str(perps[base].get("minSz") or "0")
                ),
                perp_contract_size=Decimal(
                    str(perps[base].get("ctVal") or "0")
                )
                * Decimal(str(perps[base].get("ctMult") or "1")),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        spot, swap = await asyncio.gather(
            self.http.get("/api/v5/market/tickers", instType="SPOT"),
            self.http.get("/api/v5/market/tickers", instType="SWAP"),
        )
        spots = {item["instId"]: item for item in spot.get("data", [])}
        swaps = {item["instId"]: item for item in swap.get("data", [])}
        results: list[MarketQuote] = []
        for pair in pairs:
            if pair.spot_symbol not in spots or pair.perp_symbol not in swaps:
                continue
            s, p = spots[pair.spot_symbol], swaps[pair.perp_symbol]
            required = (
                s.get("ts"), s.get("bidPx"), s.get("bidSz"), s.get("askPx"),
                s.get("askSz"), s.get("volCcy24h"), p.get("ts"), p.get("bidPx"),
                p.get("bidSz"), p.get("askPx"), p.get("askSz"), p.get("volCcy24h"),
            )
            if any(value in (None, "") for value in required):
                continue
            last = Decimal(p.get("last") or "0")
            results.append(
                MarketQuote(
                    exchange=Exchange.OKX,
                    base_asset=pair.base_asset,
                    observed_at=datetime.fromtimestamp(
                        min(int(s["ts"]), int(p["ts"])) / 1000, tz=UTC
                    ),
                    spot_bid=Decimal(s["bidPx"]),
                    spot_bid_qty=Decimal(s["bidSz"]),
                    spot_ask=Decimal(s["askPx"]),
                    spot_ask_qty=Decimal(s["askSz"]),
                    perp_bid=Decimal(p["bidPx"]),
                    perp_bid_qty=Decimal(p["bidSz"])
                    * pair.perp_contract_size,
                    perp_ask=Decimal(p["askPx"]),
                    perp_ask_qty=Decimal(p["askSz"])
                    * pair.perp_contract_size,
                    spot_quote_volume_24h=Decimal(s["volCcy24h"]),
                    perp_quote_volume_24h=Decimal(p["volCcy24h"]) * last,
                )
            )
        return results

    async def _current_one(self, pair: InstrumentPair) -> FundingObservation | None:
        payload = await self.http.get("/api/v5/public/funding-rate", instId=pair.perp_symbol)
        if not payload.get("data"):
            return None
        item = payload["data"][0]
        previous = int(item.get("prevFundingTime") or 0)
        current = int(item["fundingTime"])
        interval = Decimal(str((current - previous) / 3_600_000)) if previous else Decimal("8")
        return FundingObservation(
            exchange=Exchange.OKX,
            base_asset=pair.base_asset,
            rate=Decimal(item["fundingRate"]),
            funding_at=datetime.fromtimestamp(current / 1000, tz=UTC),
            observed_at=datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC),
            next_funding_at=datetime.fromtimestamp(int(item["nextFundingTime"]) / 1000, tz=UTC),
            interval_hours=interval,
        )

    async def current_funding(self, pairs: list[InstrumentPair]) -> list[FundingObservation]:
        values = await asyncio.gather(
            *(self._current_one(pair) for pair in pairs), return_exceptions=True
        )
        return [item for item in values if isinstance(item, FundingObservation)]

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        payload = await self.http.get(
            "/api/v5/public/funding-rate-history", instId=pair.perp_symbol, limit=100
        )
        results = []
        for item in payload.get("data", []):
            funding_at = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=UTC)
            if start <= funding_at <= end:
                results.append(
                    FundingObservation(
                        exchange=Exchange.OKX,
                        base_asset=pair.base_asset,
                        rate=Decimal(item.get("realizedRate") or item["fundingRate"]),
                        funding_at=funding_at,
                        observed_at=datetime.now(UTC),
                        settled=True,
                        interval_hours=pair.funding_interval_hours,
                    )
                )
        return results

    async def close(self) -> None:
        await self.http.close()
