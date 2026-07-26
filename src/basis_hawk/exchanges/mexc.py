from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.exchanges.base import ExchangeAdapter, PublicClient, as_list
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class MexcAdapter(ExchangeAdapter):
    name = "mexc"

    def __init__(self, *, timeout: float = 10) -> None:
        self.spot = PublicClient("https://api.mexc.com", timeout=timeout)
        self.perp = PublicClient(
            "https://contract.mexc.com", timeout=timeout, minimum_interval=0.11
        )

    async def instruments(self) -> list[InstrumentPair]:
        spot, contracts = await asyncio.gather(
            self.spot.get("/api/v3/exchangeInfo"), self.perp.get("/api/v1/contract/detail")
        )
        spots = {
            item["baseAsset"]: item["symbol"]
            for item in spot.get("symbols", [])
            if str(item.get("status")) in {"1", "ENABLED", "TRADING"}
            and item.get("quoteAsset") == "USDT"
        }
        perps = {
            item["baseCoin"]: item
            for item in contracts.get("data", [])
            if item.get("quoteCoin") == "USDT"
            and item.get("settleCoin") == "USDT"
            and item.get("state", 0) in {0, "0"}
        }
        return [
            InstrumentPair(
                exchange=Exchange.MEXC,
                base_asset=base,
                spot_symbol=spots[base],
                perp_symbol=perps[base]["symbol"],
                funding_interval_hours=Decimal(str(perps[base].get("fundingRateCycle") or 8)),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        books, tickers, contracts = await asyncio.gather(
            self.spot.get("/api/v3/ticker/bookTicker"),
            self.spot.get("/api/v3/ticker/24hr"),
            self.perp.get("/api/v1/contract/ticker"),
        )
        sb = {item["symbol"]: item for item in as_list(books)}
        st = {item["symbol"]: item for item in as_list(tickers)}
        cp = {item["symbol"]: item for item in as_list(contracts.get("data", []))}
        now = datetime.now(UTC)
        return [
            MarketQuote(
                exchange=Exchange.MEXC,
                base_asset=pair.base_asset,
                observed_at=datetime.fromtimestamp(
                    int(cp[pair.perp_symbol]["timestamp"]) / 1000, tz=UTC
                )
                if cp[pair.perp_symbol].get("timestamp")
                else now,
                spot_bid=Decimal(sb[pair.spot_symbol]["bidPrice"]),
                spot_bid_qty=Decimal(sb[pair.spot_symbol]["bidQty"]),
                spot_ask=Decimal(sb[pair.spot_symbol]["askPrice"]),
                spot_ask_qty=Decimal(sb[pair.spot_symbol]["askQty"]),
                perp_bid=Decimal(str(cp[pair.perp_symbol]["bid1"])),
                perp_bid_qty=Decimal("0"),
                perp_ask=Decimal(str(cp[pair.perp_symbol]["ask1"])),
                perp_ask_qty=Decimal("0"),
                spot_quote_volume_24h=Decimal(st[pair.spot_symbol]["quoteVolume"]),
                perp_quote_volume_24h=Decimal(str(cp[pair.perp_symbol]["amount24"])),
            )
            for pair in pairs
            if pair.spot_symbol in sb and pair.spot_symbol in st and pair.perp_symbol in cp
        ]

    async def current_funding(self, pairs: list[InstrumentPair]) -> list[FundingObservation]:
        payloads = await asyncio.gather(
            *(self.perp.get(f"/api/v1/contract/funding_rate/{pair.perp_symbol}") for pair in pairs),
            return_exceptions=True,
        )
        now = datetime.now(UTC)
        return [
            FundingObservation(
                exchange=Exchange.MEXC,
                base_asset=pair.base_asset,
                rate=Decimal(str(payload["data"]["fundingRate"])),
                funding_at=datetime.fromtimestamp(
                    int(payload["data"]["nextSettleTime"]) / 1000, tz=UTC
                ),
                observed_at=datetime.fromtimestamp(
                    int(payload["data"].get("timestamp", int(now.timestamp() * 1000))) / 1000,
                    tz=UTC,
                ),
                next_funding_at=datetime.fromtimestamp(
                    int(payload["data"]["nextSettleTime"]) / 1000, tz=UTC
                ),
                interval_hours=Decimal(str(payload["data"].get("collectCycle") or 8)),
            )
            for pair, payload in zip(pairs, payloads, strict=True)
            if isinstance(payload, dict) and payload.get("success") and payload.get("data")
        ]

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        payload = await self.perp.get(
            "/api/v1/contract/funding_rate/history",
            symbol=pair.perp_symbol,
            page_num=1,
            page_size=100,
        )
        results = []
        for item in payload.get("data", {}).get("resultList", []):
            funding_at = datetime.fromtimestamp(int(item["settleTime"]) / 1000, tz=UTC)
            if start <= funding_at <= end:
                results.append(
                    FundingObservation(
                        exchange=Exchange.MEXC,
                        base_asset=pair.base_asset,
                        rate=Decimal(str(item["fundingRate"])),
                        funding_at=funding_at,
                        observed_at=datetime.now(UTC),
                        settled=True,
                        interval_hours=pair.funding_interval_hours,
                    )
                )
        return results

    async def close(self) -> None:
        await asyncio.gather(self.spot.close(), self.perp.close())
