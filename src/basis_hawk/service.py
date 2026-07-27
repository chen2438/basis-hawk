from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from time import monotonic

from basis_hawk.calculations import build_opportunity
from basis_hawk.exchanges import (
    BinanceAdapter,
    BitgetAdapter,
    BybitAdapter,
    ExchangeAdapter,
    GateAdapter,
    MexcAdapter,
    OkxAdapter,
)
from basis_hawk.models import (
    Exchange,
    ExchangeStatus,
    FundingObservation,
    InstrumentPair,
    MarketQuote,
    Opportunity,
    Quality,
    ScannerSettings,
)
from basis_hawk.storage import Database

logger = logging.getLogger(__name__)


def default_adapters(timeout: float = 10) -> dict[Exchange, ExchangeAdapter]:
    return {
        Exchange.BINANCE: BinanceAdapter(timeout=timeout),
        Exchange.OKX: OkxAdapter(timeout=timeout),
        Exchange.MEXC: MexcAdapter(timeout=timeout),
        Exchange.BYBIT: BybitAdapter(timeout=timeout),
        Exchange.BITGET: BitgetAdapter(timeout=timeout),
        Exchange.GATE: GateAdapter(timeout=timeout),
    }


class ScannerService:
    def __init__(
        self,
        database: Database,
        adapters: dict[Exchange, ExchangeAdapter],
    ) -> None:
        self.database = database
        self.adapters = adapters
        self.settings = ScannerSettings()
        self.pairs: dict[Exchange, list[InstrumentPair]] = {exchange: [] for exchange in adapters}
        self._pairs_by_key: dict[str, InstrumentPair] = {}
        self.quotes: dict[str, MarketQuote] = {}
        self.current: dict[str, FundingObservation] = {}
        self.history: dict[str, list[FundingObservation]] = {}
        self.opportunities: dict[str, Opportunity] = {}
        self.statuses: dict[Exchange, ExchangeStatus] = {
            exchange: ExchangeStatus(exchange=exchange) for exchange in adapters
        }
        self.sequence = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._stopping = asyncio.Event()

    async def initialize(self) -> None:
        await self.database.initialize()
        self.settings = await self.database.load_settings()

    async def start(self) -> None:
        await self.initialize()
        self._stopping.clear()
        for exchange in self.adapters:
            self._tasks.append(asyncio.create_task(self._market_loop(exchange)))
            self._tasks.append(asyncio.create_task(self._funding_loop(exchange)))
            self._tasks.append(asyncio.create_task(self._history_loop(exchange)))
        self._tasks.extend(
            [asyncio.create_task(self._snapshot_loop()), asyncio.create_task(self._prune_loop())]
        )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
        await self.database.close()

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _market_loop(self, exchange: Exchange) -> None:
        catalog_due = 0.0
        while not self._stopping.is_set():
            started = monotonic()
            try:
                if monotonic() >= catalog_due:
                    await self.refresh_catalog(exchange)
                    catalog_due = monotonic() + 900
                await self.refresh_quotes(exchange)
                self.statuses[exchange] = self.statuses[exchange].model_copy(
                    update={
                        "state": "healthy",
                        "latency_ms": round((monotonic() - started) * 1000),
                        "error": None,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "exchange refresh failed", extra={"exchange": exchange.value, "error": str(exc)}
                )
                self.statuses[exchange] = self.statuses[exchange].model_copy(
                    update={"state": "degraded", "error": str(exc)[:300]}
                )
                self._mark_exchange_stale(exchange)
            await self._wait(max(0.25, 5 - (monotonic() - started)))

    async def _funding_loop(self, exchange: Exchange) -> None:
        while not self._stopping.is_set():
            if not self.pairs[exchange]:
                await self._wait(1)
                continue
            try:
                await self.refresh_current_funding(exchange)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "current funding unavailable",
                    extra={"exchange": exchange.value, "error": str(exc)},
                )
            await self._wait(60)

    async def _history_loop(self, exchange: Exchange) -> None:
        while not self._stopping.is_set():
            if not self.pairs[exchange]:
                await self._wait(1)
                continue
            adapter = self.adapters[exchange]
            end = datetime.now(UTC)
            start = end - timedelta(days=7, hours=12)
            ready = 0
            for pair in list(self.pairs[exchange]):
                if self._stopping.is_set():
                    return
                try:
                    persisted = await self.database.funding_history(
                        exchange.value, pair.base_asset, since=start
                    )
                    needs_remote = not persisted or end - persisted[0].funding_at < timedelta(
                        days=6
                    )
                    if needs_remote:
                        fetched = await adapter.funding_history(pair, start=start, end=end)
                        await self.database.save_funding(fetched)
                        persisted = await self.database.funding_history(
                            exchange.value, pair.base_asset, since=start
                        )
                    self.history[pair.key] = persisted
                    if persisted and persisted[-1].funding_at - persisted[
                        0
                    ].funding_at >= timedelta(days=6):
                        ready += 1
                    self._rebuild(pair.key)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.info(
                        "funding history unavailable",
                        extra={
                            "exchange": exchange.value,
                            "symbol": pair.base_asset,
                            "error": str(exc),
                        },
                    )
            self.statuses[exchange] = self.statuses[exchange].model_copy(
                update={"history_ready": ready}
            )
            await self._wait(900)

    async def refresh_catalog(self, exchange: Exchange) -> None:
        adapter = self.adapters[exchange]
        all_pairs = await adapter.instruments()
        all_quotes = await adapter.quotes(all_pairs)
        quote_by_key = {f"{quote.exchange.value}:{quote.base_asset}": quote for quote in all_quotes}
        eligible = [
            pair
            for pair in all_pairs
            if pair.key in quote_by_key
            and min(
                quote_by_key[pair.key].spot_quote_volume_24h,
                quote_by_key[pair.key].perp_quote_volume_24h,
            )
            >= self.settings.minimum_quote_volume
        ]
        eligible.sort(
            key=lambda pair: (
                -min(
                    quote_by_key[pair.key].spot_quote_volume_24h,
                    quote_by_key[pair.key].perp_quote_volume_24h,
                ),
                pair.base_asset,
            )
        )
        selected = eligible[: self.settings.universe_size]
        self.pairs[exchange] = selected
        self._pairs_by_key = {
            key: pair
            for key, pair in self._pairs_by_key.items()
            if pair.exchange != exchange
        }
        self._pairs_by_key.update({pair.key: pair for pair in selected})
        for pair in selected:
            self.quotes[pair.key] = quote_by_key[pair.key]
        await self.database.replace_instruments(exchange.value, selected)
        self.statuses[exchange] = self.statuses[exchange].model_copy(
            update={
                "last_catalog_at": datetime.now(UTC),
                "instruments": len(selected),
            }
        )

    async def refresh_quotes(self, exchange: Exchange) -> None:
        if not self.pairs[exchange]:
            return
        values = await self.adapters[exchange].quotes(self.pairs[exchange])
        for quote in values:
            key = f"{exchange.value}:{quote.base_asset}"
            self.quotes[key] = quote
            self._rebuild(key)
        now = datetime.now(UTC)
        self.statuses[exchange] = self.statuses[exchange].model_copy(update={"last_quote_at": now})
        await self.database.save_latest_opportunities(
            [
                item
                for item in self.opportunities.values()
                if item.exchange == exchange
            ]
        )
        await self._publish(
            [item for item in self.opportunities.values() if item.exchange == exchange]
        )

    async def refresh_current_funding(self, exchange: Exchange) -> None:
        if not self.pairs[exchange]:
            return
        values = await self.adapters[exchange].current_funding(self.pairs[exchange])
        for item in values:
            key = f"{exchange.value}:{item.base_asset}"
            self.current[key] = item
            self._rebuild(key)
        self.statuses[exchange] = self.statuses[exchange].model_copy(
            update={"last_funding_at": datetime.now(UTC)}
        )

    def _pair(self, key: str) -> InstrumentPair | None:
        indexed = self._pairs_by_key.get(key)
        if indexed is not None:
            return indexed
        exchange = Exchange(key.split(":", 1)[0])
        return next((pair for pair in self.pairs.get(exchange, []) if pair.key == key), None)

    def instrument_pair(
        self,
        exchange: Exchange,
        base_asset: str,
    ) -> InstrumentPair | None:
        return self._pair(f"{exchange.value}:{base_asset.strip().upper()}")

    async def executable_opportunity(
        self,
        exchange: Exchange,
        base_asset: str,
    ) -> Opportunity | None:
        key = f"{exchange.value}:{base_asset.strip().upper()}"
        opportunity = self.opportunities.get(key)
        pair = self._pair(key)
        quote = self.quotes.get(key)
        adapter = self.adapters.get(exchange)
        if opportunity is None or pair is None or quote is None or adapter is None:
            return opportunity
        executable_quote = await adapter.executable_quote(pair, quote)
        if executable_quote != quote:
            self.quotes[key] = executable_quote
            self._rebuild(key)
        return self.opportunities.get(key)

    def _rebuild(self, key: str) -> None:
        pair = self._pair(key)
        if not pair or key not in self.quotes or key not in self.current:
            return
        self.opportunities[key] = build_opportunity(
            pair,
            self.quotes[key],
            self.current[key],
            self.history.get(key, []),
            self.settings.fees[pair.exchange],
            holding_days=self.settings.holding_period_days,
        )

    def _mark_exchange_stale(self, exchange: Exchange) -> None:
        for key, item in list(self.opportunities.items()):
            if item.exchange == exchange:
                self.opportunities[key] = item.model_copy(update={"quality": Quality.STALE})

    def list_opportunities(self) -> list[Opportunity]:
        now = datetime.now(UTC)
        values = []
        for item in self.opportunities.values():
            if now - item.observed_at > timedelta(seconds=15):
                item = item.model_copy(update={"quality": Quality.STALE})
            values.append(item)
        rank = {Quality.HEALTHY: 0, Quality.WARMING: 1, Quality.STALE: 2}
        return sorted(
            values,
            key=lambda item: (
                rank[item.quality],
                -(item.net_return if item.net_return is not None else item.current_apr),
                item.exchange.value,
                item.base_asset,
            ),
        )

    async def update_settings(self, settings: ScannerSettings) -> ScannerSettings:
        self.settings = settings
        await self.database.save_settings(settings)
        for key in list(self.opportunities):
            self._rebuild(key)
        await self._publish(self.list_opportunities())
        return settings

    async def _snapshot_loop(self) -> None:
        saved_minute: datetime | None = None
        while not self._stopping.is_set():
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            if saved_minute != now and self.opportunities:
                await self.database.save_snapshots(self.list_opportunities())
                saved_minute = now
            await self._wait(1)

    async def _prune_loop(self) -> None:
        while not self._stopping.is_set():
            while True:
                pruned = await self.database.prune(self.settings.retention_days)
                if not pruned:
                    break
            await self._wait(86400)

    async def _publish(self, values: Iterable[Opportunity]) -> None:
        self.sequence += 1
        message: dict[str, object] = {
            "type": "update",
            "sequence": self.sequence,
            "items": [item.model_dump(mode="json") for item in values],
        }
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._subscribers.discard(queue)
