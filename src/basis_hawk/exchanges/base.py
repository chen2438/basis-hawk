from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from time import monotonic
from typing import Any

import httpx

from basis_hawk.models import FundingObservation, InstrumentPair, MarketQuote


class PublicClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10,
        minimum_interval: float = 0.05,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._owned = client is None
        self.minimum_interval = minimum_interval
        self._next_request = 0.0
        self._lock = asyncio.Lock()

    async def get(self, path: str, **params: object) -> Any:
        error: Exception | None = None
        for attempt in range(3):
            async with self._lock:
                delay = self._next_request - monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_request = monotonic() + self.minimum_interval
            try:
                response = await self.client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                error = exc
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError(f"public request failed: {path}: {error}") from error

    async def close(self) -> None:
        if self._owned:
            await self.client.aclose()


class ExchangeAdapter(ABC):
    name: str

    @abstractmethod
    async def instruments(self) -> list[InstrumentPair]: ...

    @abstractmethod
    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]: ...

    @abstractmethod
    async def current_funding(self, pairs: list[InstrumentPair]) -> list[FundingObservation]: ...

    @abstractmethod
    async def funding_history(
        self, pair: InstrumentPair, *, start: datetime, end: datetime
    ) -> list[FundingObservation]: ...

    @abstractmethod
    async def close(self) -> None: ...


def as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
