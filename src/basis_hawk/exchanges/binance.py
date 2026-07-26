from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.exchanges.base import (
    ExchangeAdapter,
    PublicClient,
    as_list,
    filter_decimal,
)
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class BinanceAdapter(ExchangeAdapter):
    name = "binance"

    def __init__(self, *, timeout: float = 10) -> None:
        self.spot = PublicClient("https://api.binance.com", timeout=timeout)
        self.perp = PublicClient("https://fapi.binance.com", timeout=timeout)

    async def instruments(self) -> list[InstrumentPair]:
        spot_payload, perp_payload = await asyncio.gather(
            self.spot.get("/api/v3/exchangeInfo"), self.perp.get("/fapi/v1/exchangeInfo")
        )
        spots = {
            item["baseAsset"]: item
            for item in spot_payload.get("symbols", [])
            if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT"
        }
        perps = {
            item["baseAsset"]: item
            for item in perp_payload.get("symbols", [])
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("marginAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
        }
        return [
            InstrumentPair(
                exchange=Exchange.BINANCE,
                base_asset=base,
                spot_symbol=spots[base]["symbol"],
                perp_symbol=perps[base]["symbol"],
                spot_price_increment=filter_decimal(
                    spots[base], "PRICE_FILTER", "tickSize"
                ),
                spot_quantity_increment=filter_decimal(
                    spots[base], "LOT_SIZE", "stepSize"
                ),
                spot_min_quantity=filter_decimal(
                    spots[base], "LOT_SIZE", "minQty"
                ),
                spot_min_notional=(
                    filter_decimal(spots[base], "NOTIONAL", "minNotional")
                    or filter_decimal(
                        spots[base], "MIN_NOTIONAL", "minNotional"
                    )
                ),
                perp_price_increment=filter_decimal(
                    perps[base], "PRICE_FILTER", "tickSize"
                ),
                perp_quantity_increment=filter_decimal(
                    perps[base], "LOT_SIZE", "stepSize"
                ),
                perp_min_quantity=filter_decimal(
                    perps[base], "LOT_SIZE", "minQty"
                ),
                perp_min_notional=filter_decimal(
                    perps[base], "MIN_NOTIONAL", "notional"
                ),
                perp_contract_size=Decimal("1"),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        spot_books, spot_tickers, perp_books, perp_tickers = await asyncio.gather(
            self.spot.get("/api/v3/ticker/bookTicker"),
            self.spot.get("/api/v3/ticker/24hr"),
            self.perp.get("/fapi/v1/ticker/bookTicker"),
            self.perp.get("/fapi/v1/ticker/24hr"),
        )
        sb = {item["symbol"]: item for item in as_list(spot_books)}
        st = {item["symbol"]: item for item in as_list(spot_tickers)}
        pb = {item["symbol"]: item for item in as_list(perp_books)}
        pt = {item["symbol"]: item for item in as_list(perp_tickers)}
        now = datetime.now(UTC)
        results: list[MarketQuote] = []
        for pair in pairs:
            if pair.spot_symbol not in sb or pair.perp_symbol not in pb:
                continue
            results.append(
                MarketQuote(
                    exchange=Exchange.BINANCE,
                    base_asset=pair.base_asset,
                    observed_at=now,
                    spot_bid=Decimal(sb[pair.spot_symbol]["bidPrice"]),
                    spot_bid_qty=Decimal(sb[pair.spot_symbol]["bidQty"]),
                    spot_ask=Decimal(sb[pair.spot_symbol]["askPrice"]),
                    spot_ask_qty=Decimal(sb[pair.spot_symbol]["askQty"]),
                    perp_bid=Decimal(pb[pair.perp_symbol]["bidPrice"]),
                    perp_bid_qty=Decimal(pb[pair.perp_symbol]["bidQty"]),
                    perp_ask=Decimal(pb[pair.perp_symbol]["askPrice"]),
                    perp_ask_qty=Decimal(pb[pair.perp_symbol]["askQty"]),
                    spot_quote_volume_24h=Decimal(st[pair.spot_symbol]["quoteVolume"]),
                    perp_quote_volume_24h=Decimal(pt[pair.perp_symbol]["quoteVolume"]),
                )
            )
        return results

    async def current_funding(self, pairs: list[InstrumentPair]) -> list[FundingObservation]:
        premiums = as_list(await self.perp.get("/fapi/v1/premiumIndex"))
        by_symbol = {item["symbol"]: item for item in premiums}
        try:
            infos = as_list(await self.perp.get("/fapi/v1/fundingInfo"))
        except RuntimeError:
            infos = []
        intervals = {item["symbol"]: Decimal(str(item["fundingIntervalHours"])) for item in infos}
        now = datetime.now(UTC)
        return [
            FundingObservation(
                exchange=Exchange.BINANCE,
                base_asset=pair.base_asset,
                rate=Decimal(by_symbol[pair.perp_symbol]["lastFundingRate"]),
                funding_at=datetime.fromtimestamp(
                    int(by_symbol[pair.perp_symbol]["time"]) / 1000, tz=UTC
                ),
                observed_at=now,
                next_funding_at=datetime.fromtimestamp(
                    int(by_symbol[pair.perp_symbol]["nextFundingTime"]) / 1000, tz=UTC
                ),
                interval_hours=intervals.get(pair.perp_symbol, pair.funding_interval_hours),
            )
            for pair in pairs
            if pair.perp_symbol in by_symbol
        ]

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        payload = as_list(
            await self.perp.get(
                "/fapi/v1/fundingRate",
                symbol=pair.perp_symbol,
                startTime=int(start.timestamp() * 1000),
                endTime=int(end.timestamp() * 1000),
                limit=1000,
            )
        )
        return [
            FundingObservation(
                exchange=Exchange.BINANCE,
                base_asset=pair.base_asset,
                rate=Decimal(item["fundingRate"]),
                funding_at=datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=UTC),
                observed_at=datetime.now(UTC),
                settled=True,
                interval_hours=pair.funding_interval_hours,
            )
            for item in payload
        ]

    async def close(self) -> None:
        await asyncio.gather(self.spot.close(), self.perp.close())
