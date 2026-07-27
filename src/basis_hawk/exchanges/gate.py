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
    decimal_or_zero,
)
from basis_hawk.models import Exchange, FundingObservation, InstrumentPair, MarketQuote


class GateAdapter(ExchangeAdapter):
    name = "gate"

    def __init__(self, *, timeout: float = 10) -> None:
        self.http = PublicClient(
            "https://api.gateio.ws/api/v4",
            timeout=timeout,
            minimum_interval=0.06,
        )
        self._contracts: dict[str, dict[str, Any]] = {}

    async def instruments(self) -> list[InstrumentPair]:
        spot_payload, perp_payload = await asyncio.gather(
            self.http.get("/spot/currency_pairs"),
            self.http.get("/futures/usdt/contracts"),
        )
        spots = {
            str(item["base"]): item
            for item in as_list(spot_payload)
            if item.get("quote") == "USDT"
            and item.get("trade_status") == "tradable"
            and item.get("type", "normal") != "premarket"
        }
        perps: dict[str, dict[str, Any]] = {}
        for item in as_list(perp_payload):
            symbol = str(item.get("name", ""))
            if (
                not symbol.endswith("_USDT")
                or item.get("status") != "trading"
                or item.get("type") != "direct"
                or item.get("in_delisting")
                or item.get("is_pre_market")
            ):
                continue
            perps[symbol.removesuffix("_USDT")] = item
            self._contracts[symbol] = item
        return [
            InstrumentPair(
                exchange=Exchange.GATE,
                base_asset=base,
                spot_symbol=str(spots[base]["id"]),
                perp_symbol=str(perps[base]["name"]),
                funding_interval_hours=Decimal(
                    str(perps[base].get("funding_interval") or 28_800)
                )
                / Decimal("3600"),
                spot_price_increment=decimal_increment(
                    spots[base].get("precision")
                ),
                spot_quantity_increment=decimal_increment(
                    spots[base].get("amount_precision")
                ),
                spot_min_quantity=Decimal(
                    str(spots[base].get("min_base_amount") or "0")
                ),
                spot_min_notional=Decimal(
                    str(spots[base].get("min_quote_amount") or "0")
                ),
                perp_price_increment=Decimal(
                    str(perps[base].get("order_price_round") or "0")
                ),
                perp_quantity_increment=Decimal("1"),
                perp_min_quantity=Decimal(
                    str(perps[base].get("order_size_min") or "0")
                ),
                perp_contract_size=Decimal(
                    str(perps[base].get("quanto_multiplier") or "0")
                ),
            )
            for base in sorted(spots.keys() & perps.keys())
        ]

    async def _tickers(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        spot_payload, perp_payload = await asyncio.gather(
            self.http.get("/spot/tickers"),
            self.http.get("/futures/usdt/tickers"),
        )
        return as_list(spot_payload), as_list(perp_payload)

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        spot_items, perp_items = await self._tickers()
        spots = {str(item["currency_pair"]): item for item in spot_items}
        perps = {str(item["contract"]): item for item in perp_items}
        now = datetime.now(UTC)
        results: list[MarketQuote] = []
        for pair in pairs:
            spot = spots.get(pair.spot_symbol)
            perp = perps.get(pair.perp_symbol)
            if not spot or not perp:
                continue
            try:
                spot_ask = Decimal(str(spot["lowest_ask"]))
                perp_bid = Decimal(str(perp["highest_bid"]))
                if spot_ask <= 0 or perp_bid <= 0:
                    continue
                contract = self._contracts.get(pair.perp_symbol, {})
                multiplier = Decimal(
                    str(perp.get("quanto_multiplier") or contract.get("quanto_multiplier") or "1")
                )
                results.append(
                    MarketQuote(
                        exchange=Exchange.GATE,
                        base_asset=pair.base_asset,
                        observed_at=now,
                        spot_bid=Decimal(str(spot["highest_bid"])),
                        spot_bid_qty=Decimal(str(spot.get("highest_size") or "0")),
                        spot_ask=spot_ask,
                        # Gate's bulk spot ticker omits best-level size. The later
                        # streaming order-book layer will populate executable depth.
                        spot_ask_qty=Decimal(str(spot.get("lowest_size") or "0")),
                        perp_bid=perp_bid,
                        perp_bid_qty=Decimal(str(perp["highest_size"])) * multiplier,
                        perp_ask=Decimal(str(perp["lowest_ask"])),
                        perp_ask_qty=Decimal(str(perp["lowest_size"])) * multiplier,
                        spot_quote_volume_24h=Decimal(str(spot["quote_volume"])),
                        perp_quote_volume_24h=Decimal(
                            str(
                                perp.get("volume_24h_quote")
                                or perp.get("volume_24h_usd")
                                or "0"
                            )
                        ),
                    )
                )
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
        return results

    async def executable_quote(
        self,
        pair: InstrumentPair,
        quote: MarketQuote,
    ) -> MarketQuote:
        spot_payload, perp_payload = await asyncio.gather(
            self.http.get(
                "/spot/order_book",
                currency_pair=pair.spot_symbol,
                limit=1,
                with_id="true",
            ),
            self.http.get(
                "/futures/usdt/order_book",
                contract=pair.perp_symbol,
                limit=1,
                with_id="true",
            ),
        )
        if not isinstance(spot_payload, dict) or not isinstance(perp_payload, dict):
            raise RuntimeError("Gate executable order book is unavailable")
        spot_bids = spot_payload.get("bids")
        spot_asks = spot_payload.get("asks")
        perp_bids = perp_payload.get("bids")
        perp_asks = perp_payload.get("asks")
        if not all(
            isinstance(levels, list) and levels
            for levels in (spot_bids, spot_asks, perp_bids, perp_asks)
        ):
            raise RuntimeError("Gate executable order book is unavailable")
        try:
            spot_bid = Decimal(str(spot_bids[0][0]))
            spot_bid_qty = Decimal(str(spot_bids[0][1]))
            spot_ask = Decimal(str(spot_asks[0][0]))
            spot_ask_qty = Decimal(str(spot_asks[0][1]))
            perp_bid = Decimal(str(perp_bids[0]["p"]))
            perp_bid_qty = abs(Decimal(str(perp_bids[0]["s"]))) * pair.perp_contract_size
            perp_ask = Decimal(str(perp_asks[0]["p"]))
            perp_ask_qty = abs(Decimal(str(perp_asks[0]["s"]))) * pair.perp_contract_size
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Gate executable order book is unavailable") from exc
        if min(
            spot_bid,
            spot_bid_qty,
            spot_ask,
            spot_ask_qty,
            perp_bid,
            perp_bid_qty,
            perp_ask,
            perp_ask_qty,
        ) <= 0:
            raise RuntimeError("Gate executable order book is unavailable")
        observed_at = datetime.now(UTC)
        timestamps = (
            decimal_or_zero(spot_payload.get("current")),
            decimal_or_zero(perp_payload.get("current")),
        )
        latest_timestamp = max(timestamps)
        if latest_timestamp > 0:
            if latest_timestamp > Decimal("100000000000"):
                latest_timestamp /= Decimal("1000")
            try:
                observed_at = datetime.fromtimestamp(float(latest_timestamp), tz=UTC)
            except (OverflowError, OSError, ValueError):
                pass
        return quote.model_copy(
            update={
                "observed_at": observed_at,
                "spot_bid": spot_bid,
                "spot_bid_qty": spot_bid_qty,
                "spot_ask": spot_ask,
                "spot_ask_qty": spot_ask_qty,
                "perp_bid": perp_bid,
                "perp_bid_qty": perp_bid_qty,
                "perp_ask": perp_ask,
                "perp_ask_qty": perp_ask_qty,
            }
        )

    async def current_funding(
        self, pairs: list[InstrumentPair]
    ) -> list[FundingObservation]:
        _, perp_items = await self._tickers()
        perps = {str(item["contract"]): item for item in perp_items}
        now = datetime.now(UTC)
        results: list[FundingObservation] = []
        for pair in pairs:
            item = perps.get(pair.perp_symbol)
            contract = self._contracts.get(pair.perp_symbol, {})
            if not item or item.get("funding_rate") in (None, ""):
                continue
            try:
                next_apply = contract.get("funding_next_apply")
                results.append(
                    FundingObservation(
                        exchange=Exchange.GATE,
                        base_asset=pair.base_asset,
                        rate=Decimal(str(item["funding_rate"])),
                        funding_at=now,
                        observed_at=now,
                        next_funding_at=(
                            datetime.fromtimestamp(int(next_apply), tz=UTC)
                            if next_apply
                            else None
                        ),
                        interval_hours=Decimal(
                            str(contract.get("funding_interval") or 28_800)
                        )
                        / Decimal("3600"),
                    )
                )
            except (InvalidOperation, TypeError, ValueError):
                continue
        return results

    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]:
        payload = await self.http.get(
            "/futures/usdt/funding_rate",
            contract=pair.perp_symbol,
            limit=1000,
            **{"from": int(start.timestamp()), "to": int(end.timestamp())},
        )
        results = []
        for item in as_list(payload):
            funding_at = datetime.fromtimestamp(int(item["t"]), tz=UTC)
            if start <= funding_at <= end:
                results.append(
                    FundingObservation(
                        exchange=Exchange.GATE,
                        base_asset=pair.base_asset,
                        rate=Decimal(str(item["r"])),
                        funding_at=funding_at,
                        observed_at=datetime.now(UTC),
                        settled=True,
                        interval_hours=pair.funding_interval_hours,
                    )
                )
        return sorted(results, key=lambda item: item.funding_at)

    async def close(self) -> None:
        await self.http.close()
