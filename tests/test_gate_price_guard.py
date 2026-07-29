from decimal import Decimal

import httpx
import pytest

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.gate_price_guard import (
    GatePerpPriceGuard,
    GatePriceGuardError,
    guarded_ioc_limit_price,
)


def test_guarded_buy_rejects_top_book_outside_exchange_band() -> None:
    assert guarded_ioc_limit_price(
        side="buy",
        planned_limit_price=Decimal("0.0063"),
        top_price=Decimal("0.0063"),
        mark_price=Decimal("0.0061"),
        maximum_deviation=Decimal("0.02"),
        price_increment=Decimal("0.0001"),
    ) is None


def test_guarded_limits_clamp_without_weakening_strategy_protection() -> None:
    assert guarded_ioc_limit_price(
        side="buy",
        planned_limit_price=Decimal("0.0064"),
        top_price=Decimal("0.0062"),
        mark_price=Decimal("0.0061"),
        maximum_deviation=Decimal("0.02"),
        price_increment=Decimal("0.0001"),
    ) == Decimal("0.0062")
    assert guarded_ioc_limit_price(
        side="sell",
        planned_limit_price=Decimal("0.0058"),
        top_price=Decimal("0.0060"),
        mark_price=Decimal("0.0061"),
        maximum_deviation=Decimal("0.02"),
        price_increment=Decimal("0.0001"),
    ) == Decimal("0.0060")


async def test_gate_guard_reads_fresh_contract_band_and_top_book() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/contracts/SWARMS_USDT"):
            return httpx.Response(
                200,
                json={
                    "status": "trading",
                    "in_delisting": False,
                    "mark_price": "0.0061",
                    "order_price_deviate": "0.02",
                    "order_price_round": "0.0001",
                },
            )
        return httpx.Response(
            200,
            json={
                "current": 1785323904.0,
                "asks": [{"p": "0.0062", "s": 100}],
                "bids": [{"p": "0.0061", "s": 100}],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gate.test",
    )
    guard = GatePerpPriceGuard(
        ExchangeEnvironment.SANDBOX,
        clock_s=lambda: 1785323905.0,
        client=http,
    )

    result = await guard.executable_limit(
        symbol="SWARMS_USDT",
        side="buy",
        planned_limit_price=Decimal("0.0064"),
    )

    assert result == Decimal("0.0062")
    assert requests == [
        "/api/v4/futures/usdt/contracts/SWARMS_USDT",
        "/api/v4/futures/usdt/order_book",
    ]
    await http.aclose()


async def test_gate_guard_rejects_stale_order_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/contracts/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "trading",
                    "in_delisting": False,
                    "mark_price": "1",
                    "order_price_deviate": "0.02",
                    "order_price_round": "0.01",
                },
            )
        return httpx.Response(
            200,
            json={
                "current": 100.0,
                "asks": [{"p": "1", "s": 1}],
                "bids": [{"p": "1", "s": 1}],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gate.test",
    )
    guard = GatePerpPriceGuard(
        ExchangeEnvironment.SANDBOX,
        clock_s=lambda: 120.0,
        client=http,
    )

    with pytest.raises(GatePriceGuardError, match="stale"):
        await guard.executable_limit(
            symbol="SWARMS_USDT",
            side="buy",
            planned_limit_price=Decimal("1"),
        )
    await http.aclose()
