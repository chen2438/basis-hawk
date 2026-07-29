from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

import httpx

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.gate_endpoints import gate_endpoints


class GatePriceGuardError(RuntimeError):
    pass


class PerpPriceGuard(Protocol):
    async def executable_limit(
        self,
        *,
        symbol: str,
        side: str,
        planned_limit_price: Decimal,
    ) -> Decimal | None: ...

    async def close(self) -> None: ...


class GatePerpPriceGuard:
    def __init__(
        self,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_s: Callable[[], float] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.http = client or httpx.AsyncClient(
            base_url=gate_endpoints(environment).rest,
            timeout=timeout,
        )
        self._owned = client is None
        self.clock_s = clock_s or time.time

    async def executable_limit(
        self,
        *,
        symbol: str,
        side: str,
        planned_limit_price: Decimal,
    ) -> Decimal | None:
        if (
            not re.fullmatch(r"[A-Z0-9_]{1,100}", symbol)
            or side not in {"buy", "sell"}
            or planned_limit_price <= 0
        ):
            raise GatePriceGuardError("Gate price guard input is invalid")
        try:
            contract_response, book_response = await asyncio.gather(
                self.http.get(
                    f"/api/v4/futures/usdt/contracts/{symbol}"
                ),
                self.http.get(
                    "/api/v4/futures/usdt/order_book",
                    params={
                        "contract": symbol,
                        "limit": 1,
                        "with_id": "true",
                    },
                ),
            )
            contract_response.raise_for_status()
            book_response.raise_for_status()
            contract = contract_response.json()
            book = book_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatePriceGuardError(
                "Gate price guard data is unavailable"
            ) from exc
        if (
            not isinstance(contract, dict)
            or not isinstance(book, dict)
            or contract.get("status") != "trading"
            or contract.get("in_delisting") is True
        ):
            raise GatePriceGuardError(
                "Gate contract is unavailable for guarded execution"
            )
        try:
            observed_at = float(book["current"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatePriceGuardError(
                "Gate price guard order book time is invalid"
            ) from exc
        age = self.clock_s() - observed_at
        if age < -5 or age > 15:
            raise GatePriceGuardError(
                "Gate price guard order book is stale"
            )
        try:
            mark_price = Decimal(str(contract["mark_price"]))
            maximum_deviation = Decimal(
                str(contract["order_price_deviate"])
            )
            price_increment = Decimal(
                str(contract["order_price_round"])
            )
        except (KeyError, ArithmeticError) as exc:
            raise GatePriceGuardError(
                "Gate price guard metadata is incomplete"
            ) from exc
        if (
            mark_price <= 0
            or maximum_deviation <= 0
            or maximum_deviation >= 1
            or price_increment <= 0
        ):
            raise GatePriceGuardError(
                "Gate price guard metadata is invalid"
            )
        book_side = "asks" if side == "buy" else "bids"
        levels = book.get(book_side)
        if not isinstance(levels, list) or not levels:
            return None
        level = levels[0]
        if not isinstance(level, dict):
            raise GatePriceGuardError(
                "Gate price guard order book is invalid"
            )
        try:
            top_price = Decimal(str(level["p"]))
        except (KeyError, ArithmeticError) as exc:
            raise GatePriceGuardError(
                "Gate price guard order book is invalid"
            ) from exc
        if top_price <= 0:
            return None
        return guarded_ioc_limit_price(
            side=side,
            planned_limit_price=planned_limit_price,
            top_price=top_price,
            mark_price=mark_price,
            maximum_deviation=maximum_deviation,
            price_increment=price_increment,
        )

    async def close(self) -> None:
        if self._owned:
            await self.http.aclose()


def guarded_ioc_limit_price(
    *,
    side: str,
    planned_limit_price: Decimal,
    top_price: Decimal,
    mark_price: Decimal,
    maximum_deviation: Decimal,
    price_increment: Decimal,
) -> Decimal | None:
    if (
        side not in {"buy", "sell"}
        or planned_limit_price <= 0
        or top_price <= 0
        or mark_price <= 0
        or maximum_deviation <= 0
        or maximum_deviation >= 1
        or price_increment <= 0
    ):
        raise GatePriceGuardError("Gate guarded limit inputs are invalid")
    if side == "buy":
        exchange_boundary = (
            mark_price * (Decimal("1") + maximum_deviation)
            / price_increment
        ).to_integral_value(rounding=ROUND_FLOOR) * price_increment
        guarded_limit = min(planned_limit_price, exchange_boundary)
        return guarded_limit if guarded_limit >= top_price else None
    exchange_boundary = (
        mark_price * (Decimal("1") - maximum_deviation)
        / price_increment
    ).to_integral_value(rounding=ROUND_CEILING) * price_increment
    guarded_limit = max(planned_limit_price, exchange_boundary)
    return guarded_limit if guarded_limit <= top_price else None
