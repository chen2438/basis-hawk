from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel, ConfigDict

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.gate_endpoints import gate_endpoints
from basis_hawk.models import Exchange

JsonRequester = Callable[
    [str, dict[str, object], dict[str, str] | None],
    Awaitable[object],
]


class OrderBookUnavailable(RuntimeError):
    pass


class OrderBookSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    bids: tuple[Decimal, ...]
    asks: tuple[Decimal, ...]
    observed_at: datetime

    def maker_price(self, *, side: str, level: int) -> Decimal:
        if level < 1:
            raise ValueError("order-book level must be positive")
        prices = self.bids if side == "buy" else self.asks
        if len(prices) < level:
            raise OrderBookUnavailable(
                f"order book has fewer than {level} executable levels"
            )
        return prices[level - 1]


class RestOrderBookProvider:
    def __init__(
        self,
        *,
        timeout: float = 5,
        request_json: JsonRequester | None = None,
    ) -> None:
        self.timeout = timeout
        self.request_json = request_json

    async def fetch(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        market: str,
        symbol: str,
        level: int,
    ) -> OrderBookSnapshot:
        if not 1 <= level <= 20:
            raise ValueError("order-book level must be between 1 and 20")
        url, params, headers = _request(
            exchange=exchange,
            environment=environment,
            market=market,
            symbol=symbol,
            level=level,
        )
        try:
            if self.request_json is not None:
                payload = await self.request_json(url, params, headers)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, params=params, headers=headers)
                    response.raise_for_status()
                    payload = response.json()
            bids, asks = _levels(exchange, market, payload)
        except OrderBookUnavailable:
            raise
        except (httpx.HTTPError, InvalidOperation, TypeError, ValueError) as exc:
            raise OrderBookUnavailable(
                f"{exchange.value} {market} order book is unavailable"
            ) from exc
        if len(bids) < level or len(asks) < level:
            raise OrderBookUnavailable(
                f"{exchange.value} {market} order book has insufficient depth"
            )
        if bids[0] >= asks[0]:
            raise OrderBookUnavailable(
                f"{exchange.value} {market} order book is crossed"
            )
        return OrderBookSnapshot(
            bids=tuple(bids),
            asks=tuple(asks),
            observed_at=datetime.now(UTC),
        )


def _request(
    *,
    exchange: Exchange,
    environment: ExchangeEnvironment,
    market: str,
    symbol: str,
    level: int,
) -> tuple[str, dict[str, object], dict[str, str] | None]:
    sandbox = environment == ExchangeEnvironment.SANDBOX
    if exchange == Exchange.BINANCE:
        base = (
            "https://demo-api.binance.com"
            if sandbox and market == "spot"
            else "https://demo-fapi.binance.com"
            if sandbox
            else "https://api.binance.com"
            if market == "spot"
            else "https://fapi.binance.com"
        )
        path = "/api/v3/depth" if market == "spot" else "/fapi/v1/depth"
        return f"{base}{path}", {"symbol": symbol, "limit": _standard_limit(level)}, None
    if exchange == Exchange.OKX:
        headers = {"x-simulated-trading": "1"} if sandbox else None
        return (
            "https://www.okx.com/api/v5/market/books",
            {"instId": symbol, "sz": _standard_limit(level)},
            headers,
        )
    if exchange == Exchange.MEXC:
        if sandbox:
            raise OrderBookUnavailable("MEXC sandbox order book is unsupported")
        if market == "spot":
            return (
                "https://api.mexc.com/api/v3/depth",
                {"symbol": symbol, "limit": _standard_limit(level)},
                None,
            )
        return (
            f"https://contract.mexc.com/api/v1/contract/depth/{symbol}",
            {"limit": level},
            None,
        )
    if exchange == Exchange.BYBIT:
        base = "https://api-testnet.bybit.com" if sandbox else "https://api.bybit.com"
        return (
            f"{base}/v5/market/orderbook",
            {
                "category": "spot" if market == "spot" else "linear",
                "symbol": symbol,
                "limit": level,
            },
            None,
        )
    if exchange == Exchange.BITGET:
        if market == "spot":
            return (
                "https://api.bitget.com/api/v2/spot/market/orderbook",
                {"symbol": symbol, "type": "step0", "limit": level},
                None,
            )
        return (
            "https://api.bitget.com/api/v2/mix/market/orderbook",
            {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "limit": level,
            },
            None,
        )
    if exchange == Exchange.GATE:
        base = f"{gate_endpoints(environment).rest}/api/v4"
        if market == "spot":
            return (
                f"{base}/spot/order_book",
                {"currency_pair": symbol, "limit": level, "with_id": "true"},
                None,
            )
        return (
            f"{base}/futures/usdt/order_book",
            {"contract": symbol, "limit": level, "with_id": "true"},
            None,
        )
    raise OrderBookUnavailable(f"{exchange.value} order book is unsupported")


def _standard_limit(level: int) -> int:
    if level <= 5:
        return 5
    if level <= 10:
        return 10
    return 20


def _levels(
    exchange: Exchange,
    market: str,
    payload: object,
) -> tuple[list[Decimal], list[Decimal]]:
    if not isinstance(payload, dict):
        raise OrderBookUnavailable("order-book response is not an object")
    if exchange == Exchange.OKX:
        data = payload.get("data")
        book = data[0] if isinstance(data, list) and data else None
    elif exchange == Exchange.MEXC and market == "perp":
        book = payload.get("data")
    elif exchange == Exchange.BYBIT:
        book = payload.get("result")
    elif exchange == Exchange.BITGET:
        if payload.get("code") != "00000":
            raise OrderBookUnavailable("Bitget rejected the order-book request")
        book = payload.get("data")
    else:
        book = payload
    if not isinstance(book, dict):
        raise OrderBookUnavailable("order-book payload is incomplete")
    bid_values = book.get("b" if exchange == Exchange.BYBIT else "bids")
    ask_values = book.get("a" if exchange == Exchange.BYBIT else "asks")
    bids = _prices(bid_values, reverse=True)
    asks = _prices(ask_values, reverse=False)
    if not bids or not asks:
        raise OrderBookUnavailable("order book has no executable levels")
    return bids, asks


def _prices(values: object, *, reverse: bool) -> list[Decimal]:
    if not isinstance(values, list):
        return []
    prices: set[Decimal] = set()
    for item in values:
        if isinstance(item, list) and len(item) >= 2:
            price_value, quantity_value = item[0], item[1]
        elif isinstance(item, dict):
            price_value = item.get("p")
            quantity_value = item.get("s")
        else:
            continue
        price = Decimal(str(price_value))
        quantity = abs(Decimal(str(quantity_value)))
        if price > 0 and quantity > 0:
            prices.add(price)
    return sorted(prices, reverse=reverse)
