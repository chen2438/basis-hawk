from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets
from basis_hawk.models import Exchange


class PositionMode(StrEnum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"
    UNKNOWN = "unknown"


class PrivateRequestError(RuntimeError):
    pass


class UnsupportedEnvironmentError(RuntimeError):
    pass


class UnsupportedTradingError(RuntimeError):
    pass


class LimitIocOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: Literal["spot", "perp"]
    symbol: str = Field(min_length=1, max_length=100)
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    client_order_id: str = Field(min_length=1, max_length=64)
    reduce_only: bool = False
    position_mode: PositionMode = PositionMode.UNKNOWN

    @field_validator("symbol", "client_order_id")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("order identifiers cannot contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def enforce_paired_strategy_direction(self) -> LimitIocOrder:
        if self.market == "spot":
            if self.reduce_only:
                raise ValueError("spot orders cannot be reduce-only")
            return self
        if self.position_mode == PositionMode.UNKNOWN:
            raise ValueError("perpetual position mode must be known")
        if self.side == "sell" and self.reduce_only:
            raise ValueError("opening short orders cannot be reduce-only")
        if self.side == "buy" and not self.reduce_only:
            raise ValueError("short-closing buy orders must be reduce-only")
        return self


class PerpConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    leverage: int
    isolated: bool
    position_mode: PositionMode


class OrderSubmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: Literal["spot", "perp"]
    symbol: str
    client_order_id: str
    exchange_order_id: str | None


class OrderCancellation(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: Literal["spot", "perp"]
    symbol: str
    client_order_id: str | None
    exchange_order_id: str | None
    accepted: bool


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    environment: ExchangeEnvironment
    observed_at: datetime
    spot_usdt_available: Decimal
    perp_usdt_available: Decimal
    perp_usdt_equity: Decimal
    shared_balance: bool
    account_mode: str
    position_mode: PositionMode
    trade_permission: bool | None

    @field_serializer(
        "spot_usdt_available",
        "perp_usdt_available",
        "perp_usdt_equity",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class RemoteOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange_order_id: str
    client_order_id: str | None = None
    market: str
    symbol: str
    side: str
    status: str
    price: Decimal
    original_quantity: Decimal
    filled_quantity: Decimal
    reduce_only: bool = False

    @field_serializer(
        "price",
        "original_quantity",
        "filled_quantity",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class RemotePosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None = None
    leverage: Decimal
    isolated: bool | None = None

    @field_serializer(
        "quantity",
        "entry_price",
        "mark_price",
        "liquidation_price",
        "leverage",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class RemoteFill(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange_trade_id: str
    exchange_order_id: str
    client_order_id: str | None = None
    market: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_asset: str
    liquidity: str
    occurred_at: datetime

    @field_serializer(
        "quantity",
        "price",
        "fee_amount",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class RemoteFillBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    fills: list[RemoteFill]
    complete: bool
    incomplete_reason: str | None = None


class RemoteOrderLookup(BaseModel):
    model_config = ConfigDict(frozen=True)

    order: RemoteOrder | None
    complete: bool
    incomplete_reason: str | None = None


class RemoteTradingState(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    environment: ExchangeEnvironment
    observed_at: datetime
    open_orders: list[RemoteOrder]
    positions: list[RemotePosition]
    complete: bool
    incomplete_reason: str | None = None


def _query(params: dict[str, object]) -> str:
    return urlencode(sorted((key, str(value)) for key, value in params.items()))


def _ordered(params: dict[str, object]) -> dict[str, object]:
    return dict(sorted(params.items()))


def _hmac_hex(secret: str, value: str, algorithm: str = "sha256") -> str:
    return hmac.new(
        secret.encode(),
        value.encode(),
        getattr(hashlib, algorithm),
    ).hexdigest()


def _hmac_base64(secret: str, value: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), value.encode(), hashlib.sha256).digest()
    ).decode()


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, object] | None = None,
    data: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
    content: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        response = await client.request(
            method,
            path,
            params=params,
            data=data,
            json=json_body,
            content=content,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise PrivateRequestError("private account request failed") from exc
    if not response.is_success:
        raise PrivateRequestError(
            f"private account request rejected with HTTP {response.status_code}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PrivateRequestError("private account response is not JSON") from exc


class PrivateAccountClient(ABC):
    exchange: Exchange

    @abstractmethod
    async def snapshot(self) -> AccountSnapshot: ...

    @abstractmethod
    async def trading_state(self) -> RemoteTradingState: ...

    @abstractmethod
    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch: ...

    @abstractmethod
    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup: ...

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        raise UnsupportedTradingError(
            f"{self.exchange.value} order placement is not implemented"
        )

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        raise UnsupportedTradingError(
            f"{self.exchange.value} order cancellation is not implemented"
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        raise UnsupportedTradingError(
            f"{self.exchange.value} perpetual configuration is not implemented"
        )

    @abstractmethod
    async def close(self) -> None: ...


class BinanceAccountClient(PrivateAccountClient):
    exchange = Exchange.BINANCE

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_ms: Callable[[], int] | None = None,
        spot_client: httpx.AsyncClient | None = None,
        perp_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        spot_url = (
            "https://testnet.binance.vision"
            if environment == ExchangeEnvironment.SANDBOX
            else "https://api.binance.com"
        )
        perp_url = (
            "https://testnet.binancefuture.com"
            if environment == ExchangeEnvironment.SANDBOX
            else "https://fapi.binance.com"
        )
        self.spot = spot_client or httpx.AsyncClient(base_url=spot_url, timeout=timeout)
        self.perp = perp_client or httpx.AsyncClient(base_url=perp_url, timeout=timeout)
        self._owned_spot = spot_client is None
        self._owned_perp = perp_client is None

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        **params: object,
    ) -> Any:
        return await self._signed_request(client, "GET", path, **params)

    async def _signed_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **params: object,
    ) -> Any:
        values = _ordered({"recvWindow": 5000, "timestamp": self.clock_ms(), **params})
        values["signature"] = _hmac_hex(self.secrets.api_secret, _query(values))
        return await _json_request(
            client,
            method,
            path,
            params=values if method in {"GET", "DELETE"} else None,
            data=values if method == "POST" else None,
            headers={"X-MBX-APIKEY": self.secrets.api_key},
        )

    async def snapshot(self) -> AccountSnapshot:
        spot, perp, mode = await _gather(
            self._get(self.spot, "/api/v3/account"),
            self._get(self.perp, "/fapi/v3/account"),
            self._get(self.perp, "/fapi/v1/positionSide/dual"),
        )
        spot_usdt = next(
            (item for item in spot.get("balances", []) if item.get("asset") == "USDT"),
            {},
        )
        perp_usdt = next(
            (item for item in perp.get("assets", []) if item.get("asset") == "USDT"),
            {},
        )
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal(str(spot_usdt.get("free") or "0")),
            perp_usdt_available=Decimal(
                str(perp_usdt.get("availableBalance") or "0")
            ),
            perp_usdt_equity=Decimal(str(perp_usdt.get("walletBalance") or "0")),
            shared_balance=False,
            account_mode=str(spot.get("accountType") or "spot+usdt_futures"),
            position_mode=(
                PositionMode.HEDGE
                if mode.get("dualSidePosition") is True
                else PositionMode.ONE_WAY
            ),
            trade_permission=bool(spot.get("canTrade")) and bool(perp.get("canTrade")),
        )

    async def trading_state(self) -> RemoteTradingState:
        spot_orders, perp_orders, positions = await _gather(
            self._get(self.spot, "/api/v3/openOrders"),
            self._get(self.perp, "/fapi/v1/openOrders"),
            self._get(self.perp, "/fapi/v3/positionRisk"),
        )
        orders = [
            _order(
                item,
                market="spot",
                order_id="orderId",
                client_id="clientOrderId",
                quantity="origQty",
                filled="executedQty",
            )
            for item in spot_orders
        ]
        orders.extend(
            _order(
                item,
                market="perp",
                order_id="orderId",
                client_id="clientOrderId",
                quantity="origQty",
                filled="executedQty",
                reduce_only=_binance_reduce_only(item),
            )
            for item in perp_orders
        )
        normalized_positions = []
        for item in positions:
            quantity = Decimal(str(item.get("positionAmt") or "0"))
            if quantity == 0:
                continue
            normalized_positions.append(
                RemotePosition(
                    symbol=str(item.get("symbol") or ""),
                    side="long" if quantity > 0 else "short",
                    quantity=abs(quantity),
                    entry_price=Decimal(str(item.get("entryPrice") or "0")),
                    mark_price=Decimal(str(item.get("markPrice") or "0")),
                    liquidation_price=_optional_decimal(item.get("liquidationPrice")),
                    leverage=Decimal(str(item.get("leverage") or "0")),
                    isolated=str(item.get("marginType") or "").lower() == "isolated",
                )
            )
        return _state(
            self.exchange,
            self.environment,
            orders,
            normalized_positions,
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        if exchange_order_id is None:
            return _missing_order_id("Binance")
        client = self.spot if market == "spot" else self.perp
        path = "/api/v3/myTrades" if market == "spot" else "/fapi/v1/userTrades"
        items = await self._get(
            client,
            path,
            symbol=symbol,
            orderId=exchange_order_id,
            startTime=_milliseconds(since),
            limit=1000,
        )
        fills = [
            RemoteFill(
                exchange_trade_id=str(item.get("id") or ""),
                exchange_order_id=str(item.get("orderId") or exchange_order_id),
                client_order_id=client_order_id,
                market=market,
                symbol=str(item.get("symbol") or symbol),
                side=(
                    "buy"
                    if item.get("isBuyer") is True or item.get("buyer") is True
                    else "sell"
                ),
                quantity=Decimal(str(item.get("qty") or "0")),
                price=Decimal(str(item.get("price") or "0")),
                fee_amount=Decimal(str(item.get("commission") or "0")),
                fee_asset=str(item.get("commissionAsset") or ""),
                liquidity="maker" if item.get("isMaker") is True else "taker",
                occurred_at=_from_milliseconds(item.get("time")),
            )
            for item in items
            if str(item.get("orderId") or "") == exchange_order_id
        ]
        return _fill_batch(fills, limit_reached=len(items) >= 1000, exchange="Binance")

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        client = self.spot if market == "spot" else self.perp
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        item = await self._get(
            client,
            path,
            symbol=symbol,
            origClientOrderId=client_order_id,
        )
        return RemoteOrderLookup(
            order=_order(
                item,
                market=market,
                order_id="orderId",
                client_id="clientOrderId",
                quantity="origQty",
                filled="executedQty",
                reduce_only=(
                    _binance_reduce_only(item) if market == "perp" else False
                ),
            ),
            complete=True,
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if len(order.client_order_id) > 36:
            raise ValueError("Binance client order IDs cannot exceed 36 characters")
        client = self.spot if order.market == "spot" else self.perp
        path = "/api/v3/order" if order.market == "spot" else "/fapi/v1/order"
        params: dict[str, object] = {
            "symbol": order.symbol,
            "side": order.side.upper(),
            "type": "LIMIT",
            "timeInForce": "IOC",
            "quantity": format(order.quantity, "f"),
            "price": format(order.limit_price, "f"),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if order.market == "perp":
            if order.position_mode == PositionMode.HEDGE:
                params["positionSide"] = "SHORT"
            else:
                params["positionSide"] = "BOTH"
                params["reduceOnly"] = str(order.reduce_only).lower()
        item = await self._signed_request(client, "POST", path, **params)
        return OrderSubmission(
            market=order.market,
            symbol=str(item.get("symbol") or order.symbol),
            client_order_id=str(
                item.get("clientOrderId") or order.client_order_id
            ),
            exchange_order_id=(
                str(item["orderId"]) if item.get("orderId") is not None else None
            ),
        )

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("an exchange or client order ID is required")
        client = self.spot if market == "spot" else self.perp
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        params: dict[str, object] = {"symbol": symbol}
        if exchange_order_id is not None:
            params["orderId"] = exchange_order_id
        else:
            params["origClientOrderId"] = client_order_id or ""
        item = await self._signed_request(client, "DELETE", path, **params)
        return OrderCancellation(
            market=market,
            symbol=str(item.get("symbol") or symbol),
            client_order_id=(
                str(item["clientOrderId"])
                if item.get("clientOrderId")
                else client_order_id
            ),
            exchange_order_id=(
                str(item["orderId"]) if item.get("orderId") is not None else None
            ),
            accepted=True,
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        if leverage < 1 or leverage > 10:
            raise ValueError("leverage must be between 1 and 10")
        if position_mode == PositionMode.UNKNOWN:
            raise ValueError("position mode must be known before configuration")
        orders, positions = await _gather(
            self._get(self.perp, "/fapi/v1/openOrders", symbol=symbol),
            self._get(self.perp, "/fapi/v3/positionRisk", symbol=symbol),
        )
        isolated = bool(positions) and all(
            str(item.get("marginType") or "").lower() == "isolated"
            for item in positions
        )
        if not isolated:
            has_position = any(
                Decimal(str(item.get("positionAmt") or "0")) != 0
                for item in positions
            )
            if orders or has_position:
                raise PrivateRequestError(
                    "cannot change Binance margin type with open orders or positions"
                )
            result = await self._signed_request(
                self.perp,
                "POST",
                "/fapi/v1/marginType",
                symbol=symbol,
                marginType="ISOLATED",
            )
            if int(result.get("code") or 0) != 200:
                raise PrivateRequestError("Binance isolated margin configuration failed")
        result = await self._signed_request(
            self.perp,
            "POST",
            "/fapi/v1/leverage",
            symbol=symbol,
            leverage=leverage,
        )
        if int(result.get("leverage") or 0) != leverage:
            raise PrivateRequestError("Binance leverage configuration was not confirmed")
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def close(self) -> None:
        if self._owned_spot:
            await self.spot.aclose()
        if self._owned_perp:
            await self.perp.aclose()


class OkxAccountClient(PrivateAccountClient):
    exchange = Exchange.OKX

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock: Callable[[], datetime] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not secrets.passphrase:
            raise ValueError("OKX requires a passphrase")
        self.secrets = secrets
        self.environment = environment
        self.clock = clock or (lambda: datetime.now(UTC))
        self.http = client or httpx.AsyncClient(base_url="https://www.okx.com", timeout=timeout)
        self._owned = client is None

    async def _get(self, path: str, **params: object) -> Any:
        params = _ordered(params)
        query = _query(params)
        request_path = f"{path}?{query}" if query else path
        headers = self._headers("GET", request_path)
        return await _json_request(
            self.http,
            "GET",
            path,
            params=params,
            headers=headers,
        )

    async def _post(self, path: str, **values: object) -> Any:
        body = _ordered(values)
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return await _json_request(
            self.http,
            "POST",
            path,
            content=content,
            headers=self._headers("POST", path, body=content),
        )

    def _headers(
        self,
        method: str,
        request_path: str,
        *,
        body: str = "",
    ) -> dict[str, str]:
        timestamp = self.clock().astimezone(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        headers = {
            "OK-ACCESS-KEY": self.secrets.api_key,
            "OK-ACCESS-SIGN": _hmac_base64(
                self.secrets.api_secret,
                f"{timestamp}{method}{request_path}{body}",
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.secrets.passphrase or "",
            "Content-Type": "application/json",
        }
        if self.environment == ExchangeEnvironment.SANDBOX:
            headers["x-simulated-trading"] = "1"
        return headers

    async def snapshot(self) -> AccountSnapshot:
        balance, config = await _gather(
            self._get("/api/v5/account/balance", ccy="USDT"),
            self._get("/api/v5/account/config"),
        )
        _okx_success(balance)
        _okx_success(config)
        account = (balance.get("data") or [{}])[0]
        details = account.get("details") or []
        usdt = next((item for item in details if item.get("ccy") == "USDT"), {})
        configuration = (config.get("data") or [{}])[0]
        available = Decimal(str(usdt.get("availBal") or "0"))
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=available,
            perp_usdt_available=available,
            perp_usdt_equity=Decimal(str(usdt.get("eq") or "0")),
            shared_balance=True,
            account_mode=f"acctLv:{configuration.get('acctLv', 'unknown')}",
            position_mode=(
                PositionMode.HEDGE
                if configuration.get("posMode") == "long_short_mode"
                else PositionMode.ONE_WAY
                if configuration.get("posMode") == "net_mode"
                else PositionMode.UNKNOWN
            ),
            trade_permission=None,
        )

    async def trading_state(self) -> RemoteTradingState:
        pending, positions = await _gather(
            self._get("/api/v5/trade/orders-pending"),
            self._get("/api/v5/account/positions", instType="SWAP"),
        )
        _okx_success(pending)
        _okx_success(positions)
        order_items = pending.get("data") or []
        position_items = positions.get("data") or []
        orders = [
            RemoteOrder(
                exchange_order_id=str(item.get("ordId") or ""),
                client_order_id=str(item["clOrdId"]) if item.get("clOrdId") else None,
                market="spot" if item.get("instType") == "SPOT" else "perp",
                symbol=str(item.get("instId") or ""),
                side=str(item.get("side") or "").lower(),
                status=str(item.get("state") or ""),
                price=Decimal(str(item.get("px") or "0")),
                original_quantity=Decimal(str(item.get("sz") or "0")),
                filled_quantity=Decimal(str(item.get("accFillSz") or "0")),
                reduce_only=_okx_reduce_only(item),
            )
            for item in order_items
        ]
        normalized_positions = []
        for item in position_items:
            quantity = Decimal(str(item.get("pos") or "0"))
            if quantity == 0:
                continue
            side = str(item.get("posSide") or "net").lower()
            if side == "net":
                side = "long" if quantity > 0 else "short"
            normalized_positions.append(
                RemotePosition(
                    symbol=str(item.get("instId") or ""),
                    side=side,
                    quantity=abs(quantity),
                    entry_price=Decimal(str(item.get("avgPx") or "0")),
                    mark_price=Decimal(str(item.get("markPx") or "0")),
                    liquidation_price=_optional_decimal(item.get("liqPx")),
                    leverage=Decimal(str(item.get("lever") or "0")),
                    isolated=item.get("mgnMode") == "isolated",
                )
            )
        complete = len(order_items) < 100 and len(position_items) < 100
        return _state(
            self.exchange,
            self.environment,
            orders,
            normalized_positions,
            complete=complete,
            incomplete_reason=None if complete else "OKX reconciliation result may be paginated",
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        params: dict[str, object] = {
            "instType": "SPOT" if market == "spot" else "SWAP",
            "instId": symbol,
            "begin": _milliseconds(since),
            "limit": 100,
        }
        if exchange_order_id:
            params["ordId"] = exchange_order_id
        payload = await self._get("/api/v5/trade/fills-history", **params)
        _okx_success(payload)
        items = payload.get("data") or []
        fills = [
            RemoteFill(
                exchange_trade_id=str(item.get("tradeId") or item.get("billId") or ""),
                exchange_order_id=str(item.get("ordId") or exchange_order_id or ""),
                client_order_id=(
                    str(item["clOrdId"])
                    if item.get("clOrdId")
                    else client_order_id
                ),
                market=market,
                symbol=str(item.get("instId") or symbol),
                side=str(item.get("side") or "").lower(),
                quantity=Decimal(str(item.get("fillSz") or "0")),
                price=Decimal(str(item.get("fillPx") or "0")),
                fee_amount=-Decimal(str(item.get("fee") or "0")),
                fee_asset=str(item.get("feeCcy") or ""),
                liquidity=(
                    "maker"
                    if str(item.get("execType") or "").upper() == "M"
                    else "taker"
                ),
                occurred_at=_from_milliseconds(item.get("ts")),
            )
            for item in items
            if (
                exchange_order_id is None
                or str(item.get("ordId") or "") == exchange_order_id
            )
            and (
                client_order_id is None
                or not item.get("clOrdId")
                or str(item.get("clOrdId")) == client_order_id
            )
        ]
        return _fill_batch(fills, limit_reached=len(items) >= 100, exchange="OKX")

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        payload = await self._get(
            "/api/v5/trade/order",
            instId=symbol,
            clOrdId=client_order_id,
        )
        _okx_success(payload)
        items = payload.get("data") or []
        if not items:
            return RemoteOrderLookup(order=None, complete=True)
        item = items[0]
        return RemoteOrderLookup(
            order=RemoteOrder(
                exchange_order_id=str(item.get("ordId") or ""),
                client_order_id=(
                    str(item["clOrdId"]) if item.get("clOrdId") else None
                ),
                market=market,
                symbol=str(item.get("instId") or symbol),
                side=str(item.get("side") or "").lower(),
                status=str(item.get("state") or ""),
                price=Decimal(str(item.get("px") or item.get("avgPx") or "0")),
                original_quantity=Decimal(str(item.get("sz") or "0")),
                filled_quantity=Decimal(str(item.get("accFillSz") or "0")),
                reduce_only=_okx_reduce_only(item),
            ),
            complete=True,
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", order.client_order_id):
            raise ValueError(
                "OKX client order IDs must be at most 32 alphanumeric characters"
            )
        values: dict[str, object] = {
            "instId": order.symbol,
            "tdMode": "cash" if order.market == "spot" else "isolated",
            "clOrdId": order.client_order_id,
            "side": order.side,
            "ordType": "ioc",
            "px": format(order.limit_price, "f"),
            "sz": format(order.quantity, "f"),
        }
        if order.market == "perp":
            if order.position_mode == PositionMode.HEDGE:
                values["posSide"] = "short"
            else:
                values["reduceOnly"] = order.reduce_only
        payload = await self._post("/api/v5/trade/order", **values)
        item = _okx_command_item(payload, "order submission")
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=str(item.get("clOrdId") or order.client_order_id),
            exchange_order_id=(
                str(item["ordId"]) if item.get("ordId") else None
            ),
        )

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("an exchange or client order ID is required")
        values: dict[str, object] = {"instId": symbol}
        if exchange_order_id is not None:
            values["ordId"] = exchange_order_id
        else:
            values["clOrdId"] = client_order_id or ""
        payload = await self._post("/api/v5/trade/cancel-order", **values)
        item = _okx_command_item(payload, "order cancellation")
        return OrderCancellation(
            market=market,
            symbol=symbol,
            client_order_id=(
                str(item["clOrdId"]) if item.get("clOrdId") else client_order_id
            ),
            exchange_order_id=(
                str(item["ordId"]) if item.get("ordId") else exchange_order_id
            ),
            accepted=True,
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        if leverage < 1 or leverage > 10:
            raise ValueError("leverage must be between 1 and 10")
        if position_mode == PositionMode.UNKNOWN:
            raise ValueError("position mode must be known before configuration")
        leverage_info = await self._get(
            "/api/v5/account/leverage-info",
            instId=symbol,
            mgnMode="isolated",
        )
        _okx_success(leverage_info)
        target_side = "short" if position_mode == PositionMode.HEDGE else "net"
        current = next(
            (
                item
                for item in leverage_info.get("data") or []
                if str(item.get("posSide") or "net") == target_side
            ),
            None,
        )
        if current is not None and Decimal(
            str(current.get("lever") or "0")
        ) == Decimal(leverage):
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        pending, positions = await _gather(
            self._get("/api/v5/trade/orders-pending", instId=symbol),
            self._get("/api/v5/account/positions", instId=symbol),
        )
        _okx_success(pending)
        _okx_success(positions)
        if (pending.get("data") or []) or any(
            Decimal(str(item.get("pos") or "0")) != 0
            for item in positions.get("data") or []
        ):
            raise PrivateRequestError(
                "cannot change OKX leverage with open orders or positions"
            )
        values: dict[str, object] = {
            "instId": symbol,
            "lever": str(leverage),
            "mgnMode": "isolated",
        }
        if position_mode == PositionMode.HEDGE:
            values["posSide"] = "short"
        payload = await self._post("/api/v5/account/set-leverage", **values)
        item = _okx_command_item(
            payload,
            "leverage configuration",
            require_subcode=False,
        )
        if Decimal(str(item.get("lever") or "0")) != Decimal(leverage):
            raise PrivateRequestError("OKX leverage configuration was not confirmed")
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def close(self) -> None:
        if self._owned:
            await self.http.aclose()


class BybitAccountClient(PrivateAccountClient):
    exchange = Exchange.BYBIT

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_ms: Callable[[], int] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.secrets = secrets
        self.environment = environment
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        url = (
            "https://api-testnet.bybit.com"
            if environment == ExchangeEnvironment.SANDBOX
            else "https://api.bybit.com"
        )
        self.http = client or httpx.AsyncClient(base_url=url, timeout=timeout)
        self._owned = client is None

    async def _get(self, path: str, **params: object) -> Any:
        params = _ordered(params)
        query = _query(params)
        return await _json_request(
            self.http,
            "GET",
            path,
            params=params,
            headers=self._headers(query),
        )

    async def _post(self, path: str, **values: object) -> Any:
        body = _ordered(values)
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return await _json_request(
            self.http,
            "POST",
            path,
            content=content,
            headers=self._headers(content),
        )

    def _headers(self, payload: str) -> dict[str, str]:
        timestamp = str(self.clock_ms())
        recv_window = "5000"
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"{timestamp}{self.secrets.api_key}{recv_window}{payload}",
        )
        return {
            "X-BAPI-API-KEY": self.secrets.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }

    async def _paged(self, path: str, **params: object) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            values = dict(params)
            if cursor:
                values["cursor"] = cursor
            payload = await self._get(path, **values)
            _bybit_success(payload)
            result = payload.get("result") or {}
            items.extend(result.get("list") or [])
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor or next_cursor == cursor:
                return items
            cursor = next_cursor

    async def snapshot(self) -> AccountSnapshot:
        wallet, info, positions = await _gather(
            self._get(
                "/v5/account/wallet-balance",
                accountType="UNIFIED",
                coin="USDT",
            ),
            self._get("/v5/account/info"),
            self._paged(
                "/v5/position/list",
                category="linear",
                settleCoin="USDT",
                limit=200,
            ),
        )
        _bybit_success(wallet)
        _bybit_success(info)
        account = ((wallet.get("result") or {}).get("list") or [{}])[0]
        coin = next(
            (item for item in account.get("coin", []) if item.get("coin") == "USDT"),
            {},
        )
        details = info.get("result") or {}
        available = Decimal(str(account.get("totalAvailableBalance") or "0"))
        spot_available = max(
            Decimal("0"),
            Decimal(str(coin.get("walletBalance") or "0"))
            - Decimal(str(coin.get("locked") or "0"))
            - Decimal(str(coin.get("spotBorrow") or "0")),
        )
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=spot_available,
            perp_usdt_available=available,
            perp_usdt_equity=Decimal(str(coin.get("equity") or "0")),
            shared_balance=True,
            account_mode=(
                f"unified:{details.get('unifiedMarginStatus', 'unknown')}:"
                f"{details.get('marginMode', 'unknown')}"
            ),
            position_mode=_bybit_position_mode(positions),
            trade_permission=None,
        )

    async def trading_state(self) -> RemoteTradingState:
        spot_orders, perp_orders, positions = await _gather(
            self._paged("/v5/order/realtime", category="spot", limit=50),
            self._paged(
                "/v5/order/realtime",
                category="linear",
                settleCoin="USDT",
                limit=50,
            ),
            self._paged(
                "/v5/position/list",
                category="linear",
                settleCoin="USDT",
                limit=200,
            ),
        )
        orders = [
            _bybit_order(item, market="spot")
            for item in spot_orders
        ]
        orders.extend(_bybit_order(item, market="perp") for item in perp_orders)
        normalized_positions = [
            RemotePosition(
                symbol=str(item.get("symbol") or ""),
                side=(
                    "long"
                    if item.get("side") == "Buy"
                    else "short"
                    if item.get("side") == "Sell"
                    else str(item.get("side") or "").lower()
                ),
                quantity=Decimal(str(item.get("size") or "0")),
                entry_price=Decimal(str(item.get("avgPrice") or "0")),
                mark_price=Decimal(str(item.get("markPrice") or "0")),
                liquidation_price=_optional_decimal(item.get("liqPrice")),
                leverage=Decimal(str(item.get("leverage") or "0")),
                isolated=(
                    None
                    if item.get("tradeMode") is None
                    else int(item.get("tradeMode") or 0) == 1
                ),
            )
            for item in positions
            if Decimal(str(item.get("size") or "0")) != 0
        ]
        return _state(
            self.exchange,
            self.environment,
            orders,
            normalized_positions,
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        params: dict[str, object] = {
            "category": "spot" if market == "spot" else "linear",
            "symbol": symbol,
            "startTime": _milliseconds(since),
            "endTime": self.clock_ms(),
            "execType": "Trade",
            "limit": 100,
        }
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif client_order_id:
            params["orderLinkId"] = client_order_id
        else:
            return _missing_order_id("Bybit")
        items = await self._paged("/v5/execution/list", **params)
        fills = [
            RemoteFill(
                exchange_trade_id=str(item.get("execId") or ""),
                exchange_order_id=str(item.get("orderId") or exchange_order_id or ""),
                client_order_id=(
                    str(item["orderLinkId"])
                    if item.get("orderLinkId")
                    else client_order_id
                ),
                market=market,
                symbol=str(item.get("symbol") or symbol),
                side=str(item.get("side") or "").lower(),
                quantity=Decimal(str(item.get("execQty") or "0")),
                price=Decimal(str(item.get("execPrice") or "0")),
                fee_amount=Decimal(str(item.get("execFee") or "0")),
                fee_asset=str(item.get("feeCurrency") or ""),
                liquidity=(
                    str(item.get("liquidity") or "").lower()
                    or ("maker" if item.get("isMaker") is True else "taker")
                ),
                occurred_at=_from_milliseconds(item.get("execTime")),
            )
            for item in items
        ]
        return RemoteFillBatch(fills=fills, complete=True)

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        params = {
            "category": "spot" if market == "spot" else "linear",
            "symbol": symbol,
            "orderLinkId": client_order_id,
            "limit": 50,
        }
        items = await self._paged("/v5/order/realtime", **params)
        if not items:
            items = await self._paged("/v5/order/history", **params)
        return RemoteOrderLookup(
            order=_bybit_order(items[0], market=market) if items else None,
            complete=True,
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", order.client_order_id):
            raise ValueError(
                "Bybit client order IDs must be at most 36 ASCII letters, numbers, "
                "dashes, or underscores"
            )
        values: dict[str, object] = {
            "category": "spot" if order.market == "spot" else "linear",
            "symbol": order.symbol,
            "side": order.side.title(),
            "orderType": "Limit",
            "qty": format(order.quantity, "f"),
            "price": format(order.limit_price, "f"),
            "timeInForce": "IOC",
            "orderLinkId": order.client_order_id,
        }
        if order.market == "spot":
            values["isLeverage"] = 0
        else:
            values["positionIdx"] = (
                2 if order.position_mode == PositionMode.HEDGE else 0
            )
            values["reduceOnly"] = order.reduce_only
        payload = await self._post("/v5/order/create", **values)
        result = _bybit_result(payload, "order submission")
        exchange_order_id = str(result.get("orderId") or "")
        if not exchange_order_id:
            raise PrivateRequestError("Bybit order submission returned no order ID")
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=str(
                result.get("orderLinkId") or order.client_order_id
            ),
            exchange_order_id=exchange_order_id,
        )

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("an exchange or client order ID is required")
        values: dict[str, object] = {
            "category": "spot" if market == "spot" else "linear",
            "symbol": symbol,
        }
        if exchange_order_id is not None:
            values["orderId"] = exchange_order_id
        else:
            values["orderLinkId"] = client_order_id or ""
        payload = await self._post("/v5/order/cancel", **values)
        result = _bybit_result(payload, "order cancellation")
        result_order_id = str(result.get("orderId") or exchange_order_id or "")
        if not result_order_id:
            raise PrivateRequestError("Bybit order cancellation returned no order ID")
        return OrderCancellation(
            market=market,
            symbol=symbol,
            client_order_id=(
                str(result["orderLinkId"])
                if result.get("orderLinkId")
                else client_order_id
            ),
            exchange_order_id=result_order_id,
            accepted=True,
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        if leverage < 1 or leverage > 10:
            raise ValueError("leverage must be between 1 and 10")
        if position_mode == PositionMode.UNKNOWN:
            raise ValueError("position mode must be known before configuration")
        info, symbol_positions = await _gather(
            self._get("/v5/account/info"),
            self._paged(
                "/v5/position/list",
                category="linear",
                symbol=symbol,
                limit=200,
            ),
        )
        _bybit_success(info)
        detected_mode = _bybit_position_mode(symbol_positions)
        if detected_mode != position_mode:
            raise PrivateRequestError(
                "Bybit position mode does not match the requested configuration"
            )
        margin_mode = str((info.get("result") or {}).get("marginMode") or "")
        target_index = 2 if position_mode == PositionMode.HEDGE else 0
        current = next(
            (
                item
                for item in symbol_positions
                if int(item.get("positionIdx") or 0) == target_index
            ),
            None,
        )
        if (
            margin_mode == "ISOLATED_MARGIN"
            and current is not None
            and Decimal(str(current.get("leverage") or "0")) == Decimal(leverage)
        ):
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        open_orders, all_positions = await _gather(
            self._paged(
                "/v5/order/realtime",
                category="linear",
                settleCoin="USDT",
                limit=50,
            ),
            self._paged(
                "/v5/position/list",
                category="linear",
                settleCoin="USDT",
                limit=200,
            ),
        )
        if open_orders or any(
            Decimal(str(item.get("size") or "0")) != 0
            for item in all_positions
        ):
            raise PrivateRequestError(
                "cannot change Bybit margin or leverage with open orders or positions"
            )
        if margin_mode != "ISOLATED_MARGIN":
            switched = await self._post(
                "/v5/account/set-margin-mode",
                setMarginMode="ISOLATED_MARGIN",
            )
            switch_result = _bybit_result(switched, "margin mode configuration")
            if switch_result.get("reasons"):
                raise PrivateRequestError(
                    "Bybit isolated margin configuration was not accepted"
                )
            confirmed = await self._get("/v5/account/info")
            _bybit_success(confirmed)
            if (
                str((confirmed.get("result") or {}).get("marginMode") or "")
                != "ISOLATED_MARGIN"
            ):
                raise PrivateRequestError(
                    "Bybit isolated margin configuration was not confirmed"
                )
        configured = await self._post(
            "/v5/position/set-leverage",
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        _bybit_result(configured, "leverage configuration")
        confirmed_positions = await self._paged(
            "/v5/position/list",
            category="linear",
            symbol=symbol,
            limit=200,
        )
        confirmed = next(
            (
                item
                for item in confirmed_positions
                if int(item.get("positionIdx") or 0) == target_index
            ),
            None,
        )
        if confirmed is None or Decimal(
            str(confirmed.get("leverage") or "0")
        ) != Decimal(leverage):
            raise PrivateRequestError("Bybit leverage configuration was not confirmed")
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def close(self) -> None:
        if self._owned:
            await self.http.aclose()


class BitgetAccountClient(PrivateAccountClient):
    exchange = Exchange.BITGET

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_ms: Callable[[], int] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not secrets.passphrase:
            raise ValueError("Bitget requires a passphrase")
        self.secrets = secrets
        self.environment = environment
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.http = client or httpx.AsyncClient(
            base_url="https://api.bitget.com",
            timeout=timeout,
        )
        self._owned = client is None

    async def _get(self, path: str, **params: object) -> Any:
        params = _ordered(params)
        query = _query(params)
        request_path = f"{path}?{query}" if query else path
        return await _json_request(
            self.http,
            "GET",
            path,
            params=params,
            headers=self._headers("GET", request_path),
        )

    async def _post(self, path: str, **values: object) -> Any:
        body = _ordered(values)
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return await _json_request(
            self.http,
            "POST",
            path,
            content=content,
            headers=self._headers("POST", path, body=content),
        )

    def _headers(
        self,
        method: str,
        request_path: str,
        *,
        body: str = "",
    ) -> dict[str, str]:
        timestamp = str(self.clock_ms())
        headers = {
            "ACCESS-KEY": self.secrets.api_key,
            "ACCESS-SIGN": _hmac_base64(
                self.secrets.api_secret,
                f"{timestamp}{method}{request_path}{body}",
            ),
            "ACCESS-PASSPHRASE": self.secrets.passphrase or "",
            "ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if self.environment == ExchangeEnvironment.SANDBOX:
            headers["paptrading"] = "1"
        return headers

    async def snapshot(self) -> AccountSnapshot:
        spot, perp = await _gather(
            self._get("/api/v2/spot/account/assets", coin="USDT"),
            self._get(
                "/api/v2/mix/account/account",
                symbol="BTCUSDT",
                productType="USDT-FUTURES",
                marginCoin="USDT",
            ),
        )
        _bitget_success(spot)
        _bitget_success(perp)
        spot_usdt = (spot.get("data") or [{}])[0]
        contract = perp.get("data") or {}
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal(str(spot_usdt.get("available") or "0")),
            perp_usdt_available=Decimal(str(contract.get("available") or "0")),
            perp_usdt_equity=Decimal(str(contract.get("accountEquity") or "0")),
            shared_balance=contract.get("assetMode") == "union",
            account_mode=(
                f"{contract.get('assetMode', 'unknown')}:"
                f"{contract.get('marginMode', 'unknown')}"
            ),
            position_mode=(
                PositionMode.HEDGE
                if contract.get("posMode") == "hedge_mode"
                else PositionMode.ONE_WAY
                if contract.get("posMode") == "one_way_mode"
                else PositionMode.UNKNOWN
            ),
            trade_permission=None,
        )

    async def trading_state(self) -> RemoteTradingState:
        spot_payload, perp_payload, position_payload = await _gather(
            self._get("/api/v2/spot/trade/unfilled-orders", limit=100),
            self._get(
                "/api/v2/mix/order/orders-pending",
                productType="USDT-FUTURES",
                limit=100,
            ),
            self._get(
                "/api/v2/mix/position/all-position",
                productType="USDT-FUTURES",
                marginCoin="USDT",
            ),
        )
        for payload in (spot_payload, perp_payload, position_payload):
            _bitget_success(payload)
        spot_orders = spot_payload.get("data") or []
        perp_data = perp_payload.get("data") or {}
        perp_orders = perp_data.get("entrustedList") or []
        position_items = position_payload.get("data") or []
        orders = [
            _bitget_order(item, market="spot")
            for item in spot_orders
        ]
        orders.extend(_bitget_order(item, market="perp") for item in perp_orders)
        positions = [
            RemotePosition(
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("holdSide") or "").lower(),
                quantity=Decimal(str(item.get("total") or "0")),
                entry_price=Decimal(str(item.get("openPriceAvg") or "0")),
                mark_price=Decimal(str(item.get("markPrice") or "0")),
                liquidation_price=_optional_decimal(item.get("liquidationPrice")),
                leverage=Decimal(str(item.get("leverage") or "0")),
                isolated=str(item.get("marginMode") or "").lower() == "isolated",
            )
            for item in position_items
            if Decimal(str(item.get("total") or "0")) != 0
        ]
        complete = len(spot_orders) < 100 and len(perp_orders) < 100
        return _state(
            self.exchange,
            self.environment,
            orders,
            positions,
            complete=complete,
            incomplete_reason=(
                None if complete else "Bitget open-order result requires another page"
            ),
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        params: dict[str, object] = {
            "symbol": symbol,
            "startTime": _milliseconds(since),
            "endTime": self.clock_ms(),
            "limit": 100,
        }
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif market == "perp" and client_order_id:
            params["clientOid"] = client_order_id
        else:
            return _missing_order_id("Bitget")
        path = (
            "/api/v2/spot/trade/fills"
            if market == "spot"
            else "/api/v2/mix/order/fill-history"
        )
        if market == "perp":
            params["productType"] = "USDT-FUTURES"
        payload = await self._get(path, **params)
        _bitget_success(payload)
        data = payload.get("data") or {}
        items = data if isinstance(data, list) else data.get("fillList") or []
        fills = [
            RemoteFill(
                exchange_trade_id=str(item.get("tradeId") or ""),
                exchange_order_id=str(item.get("orderId") or exchange_order_id or ""),
                client_order_id=(
                    str(item["clientOid"])
                    if item.get("clientOid")
                    else client_order_id
                ),
                market=market,
                symbol=str(item.get("symbol") or symbol),
                side=_bitget_side(item, market=market),
                quantity=Decimal(
                    str(
                        item.get("size")
                        or item.get("baseVolume")
                        or item.get("fillQuantity")
                        or "0"
                    )
                ),
                price=Decimal(
                    str(
                        item.get("priceAvg")
                        or item.get("price")
                        or item.get("fillPrice")
                        or "0"
                    )
                ),
                fee_amount=_bitget_fee(item),
                fee_asset=_bitget_fee_asset(item),
                liquidity=str(
                    item.get("tradeScope")
                    or item.get("tradeSide")
                    or "taker"
                ).lower(),
                occurred_at=_from_milliseconds(
                    item.get("cTime")
                    or item.get("createdTime")
                    or item.get("uTime")
                ),
            )
            for item in items
        ]
        return _fill_batch(fills, limit_reached=len(items) >= 100, exchange="Bitget")

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        if market == "spot":
            payload = await self._get(
                "/api/v2/spot/trade/orderInfo",
                symbol=symbol,
                clientOid=client_order_id,
            )
        else:
            payload = await self._get(
                "/api/v2/mix/order/detail",
                symbol=symbol,
                productType="USDT-FUTURES",
                clientOid=client_order_id,
            )
        _bitget_success(payload)
        data = payload.get("data")
        items = data if isinstance(data, list) else [data] if data else []
        return RemoteOrderLookup(
            order=_bitget_order(items[0], market=market) if items else None,
            complete=True,
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if not re.fullmatch(r"[A-Za-z0-9_:#\-+]{1,32}", order.client_order_id):
            raise ValueError(
                "Bitget client order IDs must be at most 32 supported ASCII characters"
            )
        values: dict[str, object] = {
            "symbol": order.symbol,
            "side": order.side,
            "orderType": "limit",
            "force": "ioc",
            "price": format(order.limit_price, "f"),
            "size": format(order.quantity, "f"),
            "clientOid": order.client_order_id,
        }
        path = "/api/v2/spot/trade/place-order"
        if order.market == "perp":
            path = "/api/v2/mix/order/place-order"
            values.update(
                {
                    "productType": "USDT-FUTURES",
                    "marginMode": "isolated",
                    "marginCoin": "USDT",
                }
            )
            if order.position_mode == PositionMode.HEDGE:
                values["side"] = "sell"
                values["tradeSide"] = "close" if order.reduce_only else "open"
            else:
                values["reduceOnly"] = "YES" if order.reduce_only else "NO"
        payload = await self._post(path, **values)
        result = _bitget_result(payload, "order submission")
        result_client_id = str(result.get("clientOid") or order.client_order_id)
        if not result_client_id:
            raise PrivateRequestError(
                "Bitget order submission returned no client order ID"
            )
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=result_client_id,
            exchange_order_id=(
                str(result["orderId"]) if result.get("orderId") else None
            ),
        )

    async def cancel_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> OrderCancellation:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("an exchange or client order ID is required")
        values: dict[str, object] = {"symbol": symbol}
        path = "/api/v2/spot/trade/cancel-order"
        if market == "perp":
            path = "/api/v2/mix/order/cancel-order"
            values.update(
                {"productType": "USDT-FUTURES", "marginCoin": "USDT"}
            )
        if exchange_order_id is not None:
            values["orderId"] = exchange_order_id
        else:
            values["clientOid"] = client_order_id or ""
        payload = await self._post(path, **values)
        result = _bitget_result(payload, "order cancellation")
        result_order_id = (
            str(result["orderId"]) if result.get("orderId") else exchange_order_id
        )
        result_client_id = (
            str(result["clientOid"]) if result.get("clientOid") else client_order_id
        )
        if result_order_id is None and result_client_id is None:
            raise PrivateRequestError(
                "Bitget order cancellation returned no order identifier"
            )
        return OrderCancellation(
            market=market,
            symbol=symbol,
            client_order_id=result_client_id,
            exchange_order_id=result_order_id,
            accepted=True,
        )

    async def configure_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        if leverage < 1 or leverage > 10:
            raise ValueError("leverage must be between 1 and 10")
        if position_mode == PositionMode.UNKNOWN:
            raise ValueError("position mode must be known before configuration")
        account = await self._get(
            "/api/v2/mix/account/account",
            symbol=symbol,
            productType="USDT-FUTURES",
            marginCoin="USDT",
        )
        _bitget_success(account)
        details = account.get("data") or {}
        detected_mode = (
            PositionMode.HEDGE
            if details.get("posMode") == "hedge_mode"
            else PositionMode.ONE_WAY
            if details.get("posMode") == "one_way_mode"
            else PositionMode.UNKNOWN
        )
        if detected_mode != position_mode:
            raise PrivateRequestError(
                "Bitget position mode does not match the requested configuration"
            )
        if (
            str(details.get("marginMode") or "").lower() == "isolated"
            and Decimal(str(details.get("isolatedShortLever") or "0"))
            == Decimal(leverage)
        ):
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        pending, positions = await _gather(
            self._get(
                "/api/v2/mix/order/orders-pending",
                symbol=symbol,
                productType="USDT-FUTURES",
                limit=100,
            ),
            self._get(
                "/api/v2/mix/position/all-position",
                productType="USDT-FUTURES",
                marginCoin="USDT",
            ),
        )
        _bitget_success(pending)
        _bitget_success(positions)
        pending_data = pending.get("data") or {}
        open_orders = pending_data.get("entrustedList") or []
        has_position = any(
            str(item.get("symbol") or "") == symbol
            and Decimal(str(item.get("total") or "0")) != 0
            for item in positions.get("data") or []
        )
        if open_orders or has_position:
            raise PrivateRequestError(
                "cannot change Bitget margin or leverage with open orders or positions"
            )
        if str(details.get("marginMode") or "").lower() != "isolated":
            switched = await self._post(
                "/api/v2/mix/account/set-margin-mode",
                symbol=symbol,
                productType="USDT-FUTURES",
                marginCoin="USDT",
                marginMode="isolated",
            )
            switch_result = _bitget_result(switched, "margin mode configuration")
            if str(switch_result.get("marginMode") or "").lower() != "isolated":
                raise PrivateRequestError(
                    "Bitget isolated margin configuration was not confirmed"
                )
        configured = await self._post(
            "/api/v2/mix/account/set-leverage",
            symbol=symbol,
            productType="USDT-FUTURES",
            marginCoin="USDT",
            leverage=str(leverage),
        )
        leverage_result = _bitget_result(configured, "leverage configuration")
        if (
            Decimal(str(leverage_result.get("shortLeverage") or "0"))
            != Decimal(leverage)
            or str(leverage_result.get("marginMode") or "").lower() != "isolated"
        ):
            raise PrivateRequestError(
                "Bitget leverage configuration was not confirmed"
            )
        confirmed = await self._get(
            "/api/v2/mix/account/account",
            symbol=symbol,
            productType="USDT-FUTURES",
            marginCoin="USDT",
        )
        _bitget_success(confirmed)
        confirmed_details = confirmed.get("data") or {}
        if (
            str(confirmed_details.get("marginMode") or "").lower() != "isolated"
            or Decimal(
                str(confirmed_details.get("isolatedShortLever") or "0")
            )
            != Decimal(leverage)
        ):
            raise PrivateRequestError(
                "Bitget isolated leverage was not confirmed by account state"
            )
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def close(self) -> None:
        if self._owned:
            await self.http.aclose()


class GateAccountClient(PrivateAccountClient):
    exchange = Exchange.GATE

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_s: Callable[[], int] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if environment == ExchangeEnvironment.SANDBOX:
            raise UnsupportedEnvironmentError(
                "Gate sandbox does not provide the required spot and USDT futures pair"
            )
        self.secrets = secrets
        self.environment = environment
        self.clock_s = clock_s or (lambda: int(time.time()))
        self.http = client or httpx.AsyncClient(
            base_url="https://api.gateio.ws",
            timeout=timeout,
        )
        self._owned = client is None

    async def _get(self, path: str, **params: object) -> Any:
        params = _ordered(params)
        query = _query(params)
        timestamp = str(self.clock_s())
        body_hash = hashlib.sha512(b"").hexdigest()
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"GET\n{path}\n{query}\n{body_hash}\n{timestamp}",
            "sha512",
        )
        return await _json_request(
            self.http,
            "GET",
            path,
            params=params,
            headers={
                "KEY": self.secrets.api_key,
                "Timestamp": timestamp,
                "SIGN": signature,
            },
        )

    async def snapshot(self) -> AccountSnapshot:
        spot, perp = await _gather(
            self._get("/api/v4/spot/accounts", currency="USDT"),
            self._get("/api/v4/futures/usdt/accounts"),
        )
        spot_usdt = next(
            (item for item in spot if item.get("currency") == "USDT"),
            {},
        )
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal(str(spot_usdt.get("available") or "0")),
            perp_usdt_available=Decimal(str(perp.get("available") or "0")),
            perp_usdt_equity=Decimal(str(perp.get("total") or "0")),
            shared_balance=False,
            account_mode=(
                "evolved_classic"
                if perp.get("enable_evolved_classic")
                else "classic"
            ),
            position_mode=(
                PositionMode.HEDGE
                if perp.get("in_dual_mode") is True
                else PositionMode.ONE_WAY
            ),
            trade_permission=None,
        )

    async def trading_state(self) -> RemoteTradingState:
        spot_groups, perp_orders, position_items = await _gather(
            self._get("/api/v4/spot/open_orders", page=1, limit=100),
            self._get(
                "/api/v4/futures/usdt/orders",
                status="open",
                limit=100,
                offset=0,
            ),
            self._get("/api/v4/futures/usdt/positions"),
        )
        spot_orders = [
            item
            for group in spot_groups
            for item in (group.get("orders") or [])
        ]
        orders = [_gate_spot_order(item) for item in spot_orders]
        orders.extend(_gate_perp_order(item) for item in perp_orders)
        positions = []
        for item in position_items:
            quantity = Decimal(str(item.get("size") or "0"))
            if quantity == 0:
                continue
            side = str(item.get("mode") or item.get("position_side") or "").lower()
            if side not in {"long", "short"}:
                side = "long" if quantity > 0 else "short"
            positions.append(
                RemotePosition(
                    symbol=str(item.get("contract") or ""),
                    side=side,
                    quantity=abs(quantity),
                    entry_price=Decimal(str(item.get("entry_price") or "0")),
                    mark_price=Decimal(str(item.get("mark_price") or "0")),
                    liquidation_price=_optional_decimal(item.get("liq_price")),
                    leverage=Decimal(str(item.get("leverage") or "0")),
                    isolated=(
                        None
                        if item.get("leverage") in (None, "")
                        else Decimal(str(item.get("leverage") or "0")) != 0
                    ),
                )
            )
        complete = (
            all(
                int(group.get("total") or 0) <= len(group.get("orders") or [])
                for group in spot_groups
            )
            and len(perp_orders) < 100
        )
        return _state(
            self.exchange,
            self.environment,
            orders,
            positions,
            complete=complete,
            incomplete_reason=None if complete else "Gate open-order result is paginated",
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        if exchange_order_id is None:
            return _missing_order_id("Gate")
        if market == "spot":
            items = await self._get(
                "/api/v4/spot/my_trades",
                currency_pair=symbol,
                order_id=exchange_order_id,
                limit=1000,
                page=1,
            )
        else:
            items = await self._get(
                "/api/v4/futures/usdt/my_trades",
                contract=symbol,
                order=exchange_order_id,
                limit=1000,
            )
        fills = []
        for item in items:
            quantity = Decimal(
                str(
                    item.get("amount")
                    if market == "spot"
                    else abs(Decimal(str(item.get("size") or "0")))
                )
            )
            raw_fee = Decimal(str(item.get("fee") or "0"))
            fills.append(
                RemoteFill(
                    exchange_trade_id=str(item.get("id") or ""),
                    exchange_order_id=str(
                        item.get("order_id") or exchange_order_id
                    ),
                    client_order_id=client_order_id,
                    market=market,
                    symbol=str(
                        item.get("currency_pair")
                        or item.get("contract")
                        or symbol
                    ),
                    side=(
                        str(item.get("side") or "").lower()
                        if market == "spot"
                        else "buy"
                        if Decimal(str(item.get("size") or "0")) > 0
                        else "sell"
                    ),
                    quantity=quantity,
                    price=Decimal(str(item.get("price") or "0")),
                    fee_amount=raw_fee,
                    fee_asset=str(
                        item.get("fee_currency")
                        or item.get("settle")
                        or "USDT"
                    ),
                    liquidity=str(item.get("role") or "taker").lower(),
                    occurred_at=(
                        _from_milliseconds(item.get("create_time_ms"))
                        if item.get("create_time_ms")
                        else _from_seconds(item.get("create_time"))
                    ),
                )
            )
        return _fill_batch(fills, limit_reached=len(items) >= 1000, exchange="Gate")

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        if market == "spot":
            item = await self._get(
                f"/api/v4/spot/orders/{client_order_id}",
                currency_pair=symbol,
            )
            order = _gate_spot_order(item)
        else:
            item = await self._get(
                f"/api/v4/futures/usdt/orders/{client_order_id}",
            )
            order = _gate_perp_order(item)
        return RemoteOrderLookup(order=order, complete=True)

    async def close(self) -> None:
        if self._owned:
            await self.http.aclose()


class MexcAccountClient(PrivateAccountClient):
    exchange = Exchange.MEXC

    def __init__(
        self,
        secrets: ExchangeSecrets,
        environment: ExchangeEnvironment,
        *,
        timeout: float = 10,
        clock_ms: Callable[[], int] | None = None,
        spot_client: httpx.AsyncClient | None = None,
        perp_client: httpx.AsyncClient | None = None,
    ) -> None:
        if environment == ExchangeEnvironment.SANDBOX:
            raise UnsupportedEnvironmentError("MEXC has no supported contract sandbox")
        self.secrets = secrets
        self.environment = environment
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.spot = spot_client or httpx.AsyncClient(
            base_url="https://api.mexc.com",
            timeout=timeout,
        )
        self.perp = perp_client or httpx.AsyncClient(
            base_url="https://contract.mexc.com",
            timeout=timeout,
        )
        self._owned_spot = spot_client is None
        self._owned_perp = perp_client is None

    async def _spot_get(self, path: str, **params: object) -> Any:
        values = _ordered({"recvWindow": 5000, "timestamp": self.clock_ms(), **params})
        values["signature"] = _hmac_hex(self.secrets.api_secret, _query(values))
        return await _json_request(
            self.spot,
            "GET",
            path,
            params=values,
            headers={"X-MEXC-APIKEY": self.secrets.api_key},
        )

    async def _perp_get(self, path: str, **params: object) -> Any:
        params = _ordered(params)
        query = _query(params)
        timestamp = str(self.clock_ms())
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"{self.secrets.api_key}{timestamp}{query}",
        )
        return await _json_request(
            self.perp,
            "GET",
            path,
            params=params,
            headers={
                "ApiKey": self.secrets.api_key,
                "Request-Time": timestamp,
                "Signature": signature,
                "Content-Type": "application/json",
            },
        )

    async def snapshot(self) -> AccountSnapshot:
        spot, perp, mode = await _gather(
            self._spot_get("/api/v3/account"),
            self._perp_get("/api/v1/private/account/asset/USDT"),
            self._perp_get("/api/v1/private/position/position_mode"),
        )
        if not perp.get("success") or not mode.get("success"):
            raise PrivateRequestError("MEXC private account capability probe failed")
        spot_usdt = next(
            (item for item in spot.get("balances", []) if item.get("asset") == "USDT"),
            {},
        )
        contract = perp.get("data") or {}
        return AccountSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            observed_at=datetime.now(UTC),
            spot_usdt_available=Decimal(str(spot_usdt.get("free") or "0")),
            perp_usdt_available=Decimal(
                str(contract.get("availableBalance") or "0")
            ),
            perp_usdt_equity=Decimal(str(contract.get("equity") or "0")),
            shared_balance=False,
            account_mode=str(spot.get("accountType") or "spot+contract"),
            position_mode=(
                PositionMode.HEDGE
                if mode.get("data") == 1
                else PositionMode.ONE_WAY
                if mode.get("data") == 2
                else PositionMode.UNKNOWN
            ),
            # MEXC's spot response exposes canTrade, but the contract account
            # endpoints used here do not expose an equivalent permission.
            # Do not imply that both legs are executable.
            trade_permission=None,
        )

    async def trading_state(self) -> RemoteTradingState:
        spot_orders, perp_payload, position_payload = await _gather(
            self._spot_get("/api/v3/openOrders"),
            self._perp_get(
                "/api/v1/private/order/list/open_orders",
                page_num=1,
                page_size=100,
            ),
            self._perp_get("/api/v1/private/position/open_positions"),
        )
        if not perp_payload.get("success") or not position_payload.get("success"):
            raise PrivateRequestError("MEXC trading-state reconciliation failed")
        perp_data = perp_payload.get("data") or {}
        perp_orders = (
            perp_data.get("resultList")
            if isinstance(perp_data, dict)
            else perp_data
        ) or []
        position_items = position_payload.get("data") or []
        orders = [
            _order(
                item,
                market="spot",
                order_id="orderId",
                client_id="clientOrderId",
                quantity="origQty",
                filled="executedQty",
            )
            for item in spot_orders
        ]
        orders.extend(_mexc_perp_order(item) for item in perp_orders)
        positions = [
            RemotePosition(
                symbol=str(item.get("symbol") or ""),
                side="long" if item.get("positionType") == 1 else "short",
                quantity=Decimal(str(item.get("holdVol") or "0")),
                entry_price=Decimal(str(item.get("holdAvgPrice") or "0")),
                mark_price=Decimal(str(item.get("fairPrice") or "0")),
                liquidation_price=_optional_decimal(item.get("liquidatePrice")),
                leverage=Decimal(str(item.get("leverage") or "0")),
                isolated=item.get("openType") == 1,
            )
            for item in position_items
            if Decimal(str(item.get("holdVol") or "0")) != 0
        ]
        total = int(perp_data.get("totalCount") or len(perp_orders)) if isinstance(
            perp_data, dict
        ) else len(perp_orders)
        complete = total <= len(perp_orders)
        return _state(
            self.exchange,
            self.environment,
            orders,
            positions,
            complete=complete,
            incomplete_reason=None if complete else "MEXC open-order result is paginated",
        )

    async def fills_for_order(
        self,
        *,
        market: str,
        symbol: str,
        exchange_order_id: str | None,
        client_order_id: str | None,
        since: datetime,
    ) -> RemoteFillBatch:
        if exchange_order_id is None:
            return _missing_order_id("MEXC")
        if market == "spot":
            items = await self._spot_get(
                "/api/v3/myTrades",
                symbol=symbol,
                orderId=exchange_order_id,
                startTime=_milliseconds(since),
                limit=1000,
            )
            fills = [
                RemoteFill(
                    exchange_trade_id=str(item.get("id") or ""),
                    exchange_order_id=str(item.get("orderId") or exchange_order_id),
                    client_order_id=(
                        str(item["clientOrderId"])
                        if item.get("clientOrderId")
                        else client_order_id
                    ),
                    market=market,
                    symbol=str(item.get("symbol") or symbol),
                    side="buy" if item.get("isBuyer") is True else "sell",
                    quantity=Decimal(str(item.get("qty") or "0")),
                    price=Decimal(str(item.get("price") or "0")),
                    fee_amount=Decimal(str(item.get("commission") or "0")),
                    fee_asset=str(item.get("commissionAsset") or ""),
                    liquidity="maker" if item.get("isMaker") is True else "taker",
                    occurred_at=_from_milliseconds(item.get("time")),
                )
                for item in items
            ]
            return _fill_batch(
                fills,
                limit_reached=len(items) >= 1000,
                exchange="MEXC",
            )
        payload = await self._perp_get(
            f"/api/v1/private/order/deal_details/{exchange_order_id}"
        )
        if not payload.get("success"):
            raise PrivateRequestError("MEXC fill reconciliation failed")
        items = payload.get("data") or []
        fills = [
            RemoteFill(
                exchange_trade_id=str(item.get("id") or ""),
                exchange_order_id=str(item.get("orderId") or exchange_order_id),
                client_order_id=client_order_id,
                market=market,
                symbol=str(item.get("symbol") or symbol),
                side=(
                    "buy"
                    if int(item.get("side") or 0) in {1, 2}
                    else "sell"
                ),
                quantity=Decimal(str(item.get("vol") or "0")),
                price=Decimal(str(item.get("price") or "0")),
                fee_amount=Decimal(str(item.get("fee") or "0")),
                fee_asset=str(item.get("feeCurrency") or ""),
                liquidity="taker" if item.get("taker") is True else "maker",
                occurred_at=_from_milliseconds(item.get("timestamp")),
            )
            for item in items
        ]
        return RemoteFillBatch(fills=fills, complete=True)

    async def order_by_client_id(
        self,
        *,
        market: str,
        symbol: str,
        client_order_id: str,
    ) -> RemoteOrderLookup:
        if market == "spot":
            item = await self._spot_get(
                "/api/v3/order",
                symbol=symbol,
                origClientOrderId=client_order_id,
            )
            order = _order(
                item,
                market=market,
                order_id="orderId",
                client_id="clientOrderId",
                quantity="origQty",
                filled="executedQty",
            )
        else:
            payload = await self._perp_get(
                f"/api/v1/private/order/external/{symbol}/{client_order_id}"
            )
            if not payload.get("success"):
                raise PrivateRequestError("MEXC order reconciliation failed")
            item = payload.get("data")
            order = _mexc_perp_order(item) if item else None
        return RemoteOrderLookup(order=order, complete=True)

    async def close(self) -> None:
        if self._owned_spot:
            await self.spot.aclose()
        if self._owned_perp:
            await self.perp.aclose()


def create_account_client(
    exchange: Exchange,
    secrets: ExchangeSecrets,
    environment: ExchangeEnvironment,
    *,
    timeout: float = 10,
) -> PrivateAccountClient:
    clients: dict[Exchange, type[PrivateAccountClient]] = {
        Exchange.BINANCE: BinanceAccountClient,
        Exchange.OKX: OkxAccountClient,
        Exchange.MEXC: MexcAccountClient,
        Exchange.BYBIT: BybitAccountClient,
        Exchange.BITGET: BitgetAccountClient,
        Exchange.GATE: GateAccountClient,
    }
    return clients[exchange](secrets, environment, timeout=timeout)


async def _gather(*values: Any) -> list[Any]:
    return list(await asyncio.gather(*values))


def _okx_success(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise PrivateRequestError("OKX private account request was rejected")


def _okx_command_item(
    payload: Any,
    action: str,
    *,
    require_subcode: bool = True,
) -> dict[str, Any]:
    _okx_success(payload)
    items = payload.get("data") or []
    if not items or (
        require_subcode and str(items[0].get("sCode") or "") != "0"
    ):
        raise PrivateRequestError(f"OKX {action} was rejected")
    return items[0]


def _okx_reduce_only(item: dict[str, Any]) -> bool:
    return str(item.get("reduceOnly") or "false").lower() == "true" or (
        str(item.get("posSide") or "").lower() == "short"
        and str(item.get("side") or "").lower() == "buy"
    )


def _bybit_success(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise PrivateRequestError("Bybit private account request was rejected")


def _bybit_result(payload: Any, action: str) -> dict[str, Any]:
    _bybit_success(payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PrivateRequestError(f"Bybit {action} returned no result")
    return result


def _bybit_position_mode(positions: list[dict[str, Any]]) -> PositionMode:
    indexes = {
        int(item["positionIdx"])
        for item in positions
        if item.get("positionIdx") is not None
    }
    if indexes & {1, 2}:
        return PositionMode.HEDGE
    if 0 in indexes:
        return PositionMode.ONE_WAY
    return PositionMode.UNKNOWN


def _bitget_success(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("code") != "00000":
        raise PrivateRequestError("Bitget private account request was rejected")


def _bitget_result(payload: Any, action: str) -> dict[str, Any]:
    _bitget_success(payload)
    result = payload.get("data")
    if not isinstance(result, dict):
        raise PrivateRequestError(f"Bitget {action} returned no result")
    return result


def _bitget_side(item: dict[str, Any], *, market: str) -> str:
    side = str(item.get("side") or "").lower()
    if market == "perp" and str(item.get("tradeSide") or "").lower() == "close":
        return "buy" if side == "sell" else "sell"
    return side


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _milliseconds(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _from_milliseconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(value or 0) / 1000, tz=UTC)


def _from_seconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(value or 0), tz=UTC)


def _missing_order_id(exchange: str) -> RemoteFillBatch:
    return RemoteFillBatch(
        fills=[],
        complete=False,
        incomplete_reason=(
            f"{exchange} requires an exchange order ID before fills can be reconciled"
        ),
    )


def _fill_batch(
    fills: list[RemoteFill],
    *,
    limit_reached: bool,
    exchange: str,
) -> RemoteFillBatch:
    return RemoteFillBatch(
        fills=fills,
        complete=not limit_reached,
        incomplete_reason=(
            f"{exchange} order fills reached the response limit"
            if limit_reached
            else None
        ),
    )


def _bitget_fee_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    details = item.get("feeDetail") or []
    return details if isinstance(details, list) else [details]


def _bitget_fee(item: dict[str, Any]) -> Decimal:
    details = _bitget_fee_details(item)
    if details:
        return -sum(
            (
                Decimal(
                    str(
                        detail.get("totalFee")
                        or detail.get("fee")
                        or detail.get("totalDeductionFee")
                        or "0"
                    )
                )
                for detail in details
            ),
            Decimal("0"),
        )
    return -Decimal(str(item.get("fee") or "0"))


def _bitget_fee_asset(item: dict[str, Any]) -> str:
    details = _bitget_fee_details(item)
    if details:
        return str(details[0].get("feeCoin") or item.get("marginCoin") or "")
    return str(item.get("feeCoin") or item.get("marginCoin") or "")


def _order(
    item: dict[str, Any],
    *,
    market: str,
    order_id: str,
    client_id: str,
    quantity: str,
    filled: str,
    reduce_only: bool = False,
) -> RemoteOrder:
    return RemoteOrder(
        exchange_order_id=str(item.get(order_id) or ""),
        client_order_id=str(item[client_id]) if item.get(client_id) else None,
        market=market,
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or "").lower(),
        status=str(item.get("status") or ""),
        price=Decimal(str(item.get("price") or "0")),
        original_quantity=Decimal(str(item.get(quantity) or "0")),
        filled_quantity=Decimal(str(item.get(filled) or "0")),
        reduce_only=reduce_only,
    )


def _binance_reduce_only(item: dict[str, Any]) -> bool:
    return bool(item.get("reduceOnly")) or (
        str(item.get("positionSide") or "").upper() == "SHORT"
        and str(item.get("side") or "").upper() == "BUY"
    )


def _bybit_order(item: dict[str, Any], *, market: str) -> RemoteOrder:
    return RemoteOrder(
        exchange_order_id=str(item.get("orderId") or ""),
        client_order_id=str(item["orderLinkId"]) if item.get("orderLinkId") else None,
        market=market,
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or "").lower(),
        status=str(item.get("orderStatus") or ""),
        price=Decimal(str(item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("qty") or "0")),
        filled_quantity=Decimal(str(item.get("cumExecQty") or "0")),
        reduce_only=bool(item.get("reduceOnly")),
    )


def _bitget_order(item: dict[str, Any], *, market: str) -> RemoteOrder:
    return RemoteOrder(
        exchange_order_id=str(item.get("orderId") or ""),
        client_order_id=str(item["clientOid"]) if item.get("clientOid") else None,
        market=market,
        symbol=str(item.get("symbol") or ""),
        side=_bitget_side(item, market=market),
        status=str(item.get("status") or item.get("state") or ""),
        price=Decimal(str(item.get("priceAvg") or item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("size") or "0")),
        filled_quantity=Decimal(
            str(item.get("baseVolume") or item.get("filledQty") or "0")
        ),
        reduce_only=(
            str(item.get("reduceOnly") or "").lower() == "yes"
            or str(item.get("tradeSide") or "").lower() == "close"
        ),
    )


def _gate_spot_order(item: dict[str, Any]) -> RemoteOrder:
    return RemoteOrder(
        exchange_order_id=str(item.get("id") or ""),
        client_order_id=str(item["text"]) if item.get("text") else None,
        market="spot",
        symbol=str(item.get("currency_pair") or ""),
        side=str(item.get("side") or "").lower(),
        status=str(item.get("status") or ""),
        price=Decimal(str(item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("amount") or "0")),
        filled_quantity=Decimal(str(item.get("filled_amount") or "0")),
    )


def _gate_perp_order(item: dict[str, Any]) -> RemoteOrder:
    quantity = Decimal(str(item.get("size") or "0"))
    remaining = Decimal(str(item.get("left") or "0"))
    return RemoteOrder(
        exchange_order_id=str(item.get("id") or ""),
        client_order_id=str(item["text"]) if item.get("text") else None,
        market="perp",
        symbol=str(item.get("contract") or ""),
        side="buy" if quantity > 0 else "sell",
        status=str(item.get("status") or ""),
        price=Decimal(str(item.get("price") or "0")),
        original_quantity=abs(quantity),
        filled_quantity=max(Decimal("0"), abs(quantity) - abs(remaining)),
        reduce_only=bool(item.get("is_reduce_only") or item.get("reduce_only")),
    )


def _mexc_perp_order(item: dict[str, Any]) -> RemoteOrder:
    side = int(item.get("side") or 0)
    return RemoteOrder(
        exchange_order_id=str(item.get("orderId") or ""),
        client_order_id=str(item["externalOid"]) if item.get("externalOid") else None,
        market="perp",
        symbol=str(item.get("symbol") or ""),
        side="buy" if side in {1, 2} else "sell",
        status=str(item.get("state") or ""),
        price=Decimal(str(item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("vol") or "0")),
        filled_quantity=Decimal(str(item.get("dealVol") or "0")),
        reduce_only=side in {2, 4},
    )


def _state(
    exchange: Exchange,
    environment: ExchangeEnvironment,
    orders: list[RemoteOrder],
    positions: list[RemotePosition],
    *,
    complete: bool = True,
    incomplete_reason: str | None = None,
) -> RemoteTradingState:
    return RemoteTradingState(
        exchange=exchange,
        environment=environment,
        observed_at=datetime.now(UTC),
        open_orders=orders,
        positions=positions,
        complete=complete,
        incomplete_reason=incomplete_reason,
    )
