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
from basis_hawk.gate_endpoints import gate_endpoints
from basis_hawk.models import Exchange


class PositionMode(StrEnum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"
    UNKNOWN = "unknown"


class PerpMarginMode(StrEnum):
    ISOLATED = "isolated"
    CROSS = "cross"


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


class InternalTransferSubmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    transfer_id: str
    status: Literal["pending", "completed"]


class RemoteInternalTransfer(BaseModel):
    model_config = ConfigDict(frozen=True)

    transfer_id: str
    status: Literal["pending", "completed", "failed", "unknown"]
    direction: Literal["spot_to_perp", "perp_to_spot"]
    amount: Decimal


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
    perp_margin_mode: PerpMarginMode = PerpMarginMode.ISOLATED

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


class RemoteFundingIncome(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange_record_id: str
    symbol: str
    base_asset: str
    asset: str = "USDT"
    amount: Decimal
    rate: Decimal | None = None
    position_value: Decimal | None = None
    occurred_at: datetime

    @field_serializer(
        "amount",
        "rate",
        "position_value",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class RemoteFundingIncomeBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    records: list[RemoteFundingIncome]
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


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _binance_transfer_type(direction: str) -> str:
    if direction == "spot_to_perp":
        return "MAIN_UMFUTURE"
    if direction == "perp_to_spot":
        return "UMFUTURE_MAIN"
    raise ValueError("unsupported internal transfer direction")


def _mexc_transfer_accounts(direction: str) -> tuple[str, str]:
    if direction == "spot_to_perp":
        return "SPOT", "FUTURES"
    if direction == "perp_to_spot":
        return "FUTURES", "SPOT"
    raise ValueError("unsupported internal transfer direction")


def _gate_transfer_accounts(direction: str) -> tuple[str, str]:
    if direction == "spot_to_perp":
        return "spot", "futures"
    if direction == "perp_to_spot":
        return "futures", "spot"
    raise ValueError("unsupported internal transfer direction")


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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        return RemoteFundingIncomeBatch(
            records=[],
            complete=False,
            incomplete_reason=(
                f"{self.exchange.value} funding income is not implemented"
            ),
        )

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

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        raise UnsupportedTradingError(
            f"{self.exchange.value} internal transfer is not implemented"
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        raise UnsupportedTradingError(
            f"{self.exchange.value} internal transfer lookup is not implemented"
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
        spot, perp, configuration, mode = await _gather(
            self._get(self.spot, "/api/v3/account"),
            self._get(self.perp, "/fapi/v3/account"),
            self._get(self.perp, "/fapi/v1/accountConfig"),
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
            trade_permission=bool(spot.get("canTrade"))
            and bool(configuration.get("canTrade")),
        )

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        del transfer_id
        if self.environment != ExchangeEnvironment.LIVE:
            raise UnsupportedEnvironmentError(
                "Binance internal transfer is unavailable in sandbox"
            )
        transfer_type = _binance_transfer_type(direction)
        payload = await self._signed_request(
            self.spot,
            "POST",
            "/sapi/v1/asset/transfer",
            type=transfer_type,
            asset="USDT",
            amount=format(amount, "f"),
        )
        remote_id = str(payload.get("tranId") or "")
        if not remote_id:
            raise PrivateRequestError(
                "Binance internal transfer response is incomplete"
            )
        return InternalTransferSubmission(
            transfer_id=remote_id,
            status="pending",
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        del client_transfer_id
        if self.environment != ExchangeEnvironment.LIVE:
            raise UnsupportedEnvironmentError(
                "Binance internal transfer is unavailable in sandbox"
            )
        transfer_type = _binance_transfer_type(direction)
        payload = await self._get(
            self.spot,
            "/sapi/v1/asset/transfer",
            type=transfer_type,
            startTime=int(_utc_datetime(created_at).timestamp() * 1000),
            endTime=self.clock_ms(),
            current=1,
            size=100,
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise PrivateRequestError(
                "Binance internal transfer history is incomplete"
            )
        match = next(
            (
                item
                for item in rows
                if str(item.get("tranId") or "") == transfer_id
            ),
            None,
        )
        if match is None:
            return RemoteInternalTransfer(
                transfer_id=transfer_id,
                status="unknown",
                direction=direction,
                amount=amount,
            )
        if (
            str(match.get("asset") or "") != "USDT"
            or str(match.get("type") or "") != transfer_type
            or Decimal(str(match.get("amount") or "0")) != amount
        ):
            raise PrivateRequestError(
                "Binance internal transfer history does not match the request"
            )
        raw_status = str(match.get("status") or "").upper()
        status: Literal["pending", "completed", "failed", "unknown"]
        if raw_status == "CONFIRMED":
            status = "completed"
        elif raw_status in {"PENDING", "PROCESS"}:
            status = "pending"
        elif raw_status in {"FAILED", "FAILURE"}:
            status = "failed"
        else:
            status = "unknown"
        return RemoteInternalTransfer(
            transfer_id=transfer_id,
            status=status,
            direction=direction,
            amount=amount,
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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        items = await self._get(
            self.perp,
            "/fapi/v1/income",
            incomeType="FUNDING_FEE",
            startTime=_milliseconds(since),
            limit=1000,
        )
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("tranId") or ""),
                symbol=str(item.get("symbol") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("symbol") or ""),
                ),
                asset=str(item.get("asset") or ""),
                amount=Decimal(str(item.get("income") or "0")),
                occurred_at=_from_milliseconds(item.get("time")),
            )
            for item in items
        ]
        return _funding_batch(
            records,
            limit_reached=len(items) >= 1000,
            exchange="Binance",
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
        permissions = {
            item.strip().lower()
            for item in str(configuration.get("perm") or "").split(",")
            if item.strip()
        }
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
            trade_permission=(
                "trade" in permissions if permissions else None
            ),
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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        payload = await self._get(
            "/api/v5/account/bills",
            type="8",
            begin=_milliseconds(since),
            limit=100,
        )
        _okx_success(payload)
        items = payload.get("data") or []
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("billId") or ""),
                symbol=str(item.get("instId") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("instId") or ""),
                ),
                asset=str(item.get("ccy") or ""),
                amount=Decimal(
                    str(item.get("balChg") or item.get("pnl") or "0")
                ),
                occurred_at=_from_milliseconds(item.get("ts")),
            )
            for item in items
            if str(item.get("subType") or "") in {"173", "174"}
        ]
        return _funding_batch(
            records,
            limit_reached=len(items) >= 100,
            exchange="OKX",
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
        wallet, info, positions, api_key = await _gather(
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
            self._get("/v5/user/query-api"),
        )
        _bybit_success(wallet)
        _bybit_success(info)
        _bybit_success(api_key)
        position_mode = _bybit_position_mode(positions)
        if position_mode == PositionMode.UNKNOWN:
            probe = await self._get(
                "/v5/position/list",
                category="linear",
                symbol="BTCUSDT",
                limit=200,
            )
            _bybit_success(probe)
            position_mode = _bybit_position_mode(
                (probe.get("result") or {}).get("list") or []
            )
        if (
            position_mode == PositionMode.UNKNOWN
            and self.secrets.position_mode is not None
        ):
            position_mode = PositionMode(self.secrets.position_mode)
        account = ((wallet.get("result") or {}).get("list") or [{}])[0]
        coin = next(
            (item for item in account.get("coin", []) if item.get("coin") == "USDT"),
            {},
        )
        details = info.get("result") or {}
        key_details = api_key.get("result") or {}
        raw_permissions = key_details.get("permissions")
        permissions = raw_permissions if isinstance(raw_permissions, dict) else {}
        contract_permissions = {
            str(item) for item in permissions.get("ContractTrade") or []
        }
        spot_permissions = {
            str(item) for item in permissions.get("Spot") or []
        }
        permission_known = (
            key_details.get("readOnly") is not None
            and isinstance(raw_permissions, dict)
        )
        wallet_balance = Decimal(str(coin.get("walletBalance") or "0"))
        locked = Decimal(str(coin.get("locked") or "0"))
        spot_borrow = Decimal(str(coin.get("spotBorrow") or "0"))
        total_order_im = Decimal(str(coin.get("totalOrderIM") or "0"))
        total_position_im = Decimal(str(coin.get("totalPositionIM") or "0"))
        bonus = Decimal(str(coin.get("bonus") or "0"))
        if str(details.get("marginMode") or "") == "ISOLATED_MARGIN":
            available = max(
                Decimal("0"),
                wallet_balance
                - total_position_im
                - total_order_im
                - locked
                - bonus,
            )
        else:
            available = Decimal(
                str(account.get("totalAvailableBalance") or "0")
            )
        spot_available = max(
            Decimal("0"),
            wallet_balance - locked - spot_borrow,
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
            position_mode=position_mode,
            trade_permission=(
                str(key_details.get("readOnly")) == "0"
                and "Order" in contract_permissions
                and "SpotTrade" in spot_permissions
                if permission_known
                else None
            ),
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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        payload = await self._get(
            "/v5/account/transaction-log",
            accountType="UNIFIED",
            category="linear",
            type="SETTLEMENT",
            startTime=_milliseconds(since),
            endTime=_milliseconds(datetime.now(UTC)),
            limit=50,
        )
        result = _bybit_result(payload, "funding income")
        items = result.get("list") or []
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("id") or ""),
                symbol=str(item.get("symbol") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("symbol") or ""),
                ),
                asset=str(item.get("currency") or ""),
                amount=Decimal(str(item.get("funding") or "0")),
                rate=_optional_decimal(item.get("feeRate")),
                occurred_at=_from_milliseconds(item.get("transactionTime")),
            )
            for item in items
            if Decimal(str(item.get("funding") or "0")) != 0
        ]
        return _funding_batch(
            records,
            limit_reached=bool(result.get("nextPageCursor")),
            exchange="Bybit",
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
        self._account_generation: Literal["classic", "uta"] | None = None
        self._uta_settings: dict[str, Any] | None = None

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

    async def _detect_account_generation(self) -> Literal["classic", "uta"]:
        if self._account_generation is not None:
            return self._account_generation
        settings = await self._get("/api/v3/account/settings")
        if isinstance(settings, dict) and settings.get("code") == "00000":
            details = settings.get("data")
            mode = (
                str(details.get("accountMode") or "").lower()
                if isinstance(details, dict)
                else ""
            )
            if mode in {"unified", "hybrid"}:
                self._account_generation = "uta"
                self._uta_settings = details
                return "uta"
            if mode in {"upgrading", "switching"}:
                raise PrivateRequestError(
                    "Bitget account is changing account mode; trading is blocked"
                )
        classic = await self._get(
            "/api/v2/mix/account/account",
            symbol="BTCUSDT",
            productType="USDT-FUTURES",
            marginCoin="USDT",
        )
        _bitget_success(classic)
        details = classic.get("data")
        if not isinstance(details, dict) or str(
            details.get("posMode") or ""
        ) not in {"one_way_mode", "hedge_mode"}:
            raise PrivateRequestError(
                "Bitget account generation could not be identified safely"
            )
        self._account_generation = "classic"
        return "classic"

    async def account_generation(self) -> Literal["classic", "uta"]:
        return await self._detect_account_generation()

    async def _settings(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and self._uta_settings is not None:
            return self._uta_settings
        payload = await self._get("/api/v3/account/settings")
        result = _bitget_result(payload, "UTA account settings")
        mode = str(result.get("accountMode") or "").lower()
        if mode not in {"unified", "hybrid"}:
            raise PrivateRequestError("Bitget UTA account mode is not stable")
        self._uta_settings = result
        return result

    async def snapshot(self) -> AccountSnapshot:
        generation = await self._detect_account_generation()
        if generation == "uta":
            settings, assets_payload, info_payload = await _gather(
                self._settings(refresh=True),
                self._get("/api/v3/account/assets"),
                self._get("/api/v3/account/info"),
            )
            assets = _bitget_result(assets_payload, "UTA account assets")
            info = _bitget_result(info_payload, "UTA account info")
            asset_items = assets.get("assets") or []
            usdt = next(
                (
                    item
                    for item in asset_items
                    if str(item.get("coin") or "").upper() == "USDT"
                ),
                {},
            )
            available = Decimal(str(usdt.get("available") or "0"))
            return AccountSnapshot(
                exchange=self.exchange,
                environment=self.environment,
                observed_at=datetime.now(UTC),
                spot_usdt_available=available,
                perp_usdt_available=available,
                perp_usdt_equity=Decimal(
                    str(assets.get("usdtEquity") or usdt.get("equity") or "0")
                ),
                shared_balance=True,
                account_mode=(
                    f"uta:{settings.get('accountMode', 'unknown')}:"
                    f"{settings.get('accountLevel', 'unknown')}:"
                    f"{settings.get('assetMode', 'unknown')}"
                ),
                position_mode=_bitget_position_mode(settings.get("holdMode")),
                trade_permission=_bitget_trade_permission(
                    info,
                    generation="uta",
                ),
            )
        spot, perp, info_payload = await _gather(
            self._get("/api/v2/spot/account/assets", coin="USDT"),
            self._get(
                "/api/v2/mix/account/account",
                symbol="BTCUSDT",
                productType="USDT-FUTURES",
                marginCoin="USDT",
            ),
            self._get("/api/v2/spot/account/info"),
        )
        _bitget_success(spot)
        _bitget_success(perp)
        info = _bitget_result(info_payload, "Classic account info")
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
            trade_permission=_bitget_trade_permission(
                info,
                generation="classic",
            ),
        )

    async def trading_state(self) -> RemoteTradingState:
        generation = await self._detect_account_generation()
        if generation == "uta":
            spot_payload, perp_payload, position_payload = await _gather(
                self._get(
                    "/api/v3/trade/unfilled-orders",
                    category="SPOT",
                    limit=100,
                ),
                self._get(
                    "/api/v3/trade/unfilled-orders",
                    category="USDT-FUTURES",
                    limit=100,
                ),
                self._get(
                    "/api/v3/position/current-position",
                    category="USDT-FUTURES",
                ),
            )
            for payload in (spot_payload, perp_payload, position_payload):
                _bitget_success(payload)
            spot_data = spot_payload.get("data") or {}
            perp_data = perp_payload.get("data") or {}
            position_data = position_payload.get("data") or {}
            spot_orders = spot_data.get("list") or []
            perp_orders = perp_data.get("list") or []
            position_items = position_data.get("list") or []
            orders = [
                _bitget_uta_order(item, market="spot") for item in spot_orders
            ]
            orders.extend(
                _bitget_uta_order(item, market="perp") for item in perp_orders
            )
            positions = [
                RemotePosition(
                    symbol=str(item.get("symbol") or ""),
                    side=str(item.get("posSide") or "").lower(),
                    quantity=Decimal(str(item.get("total") or "0")),
                    entry_price=Decimal(str(item.get("avgPrice") or "0")),
                    mark_price=Decimal(str(item.get("markPrice") or "0")),
                    liquidation_price=_optional_decimal(
                        item.get("liquidationPrice")
                    ),
                    leverage=Decimal(str(item.get("leverage") or "0")),
                    isolated=(
                        str(item.get("marginMode") or "").lower() == "isolated"
                    ),
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
                    None
                    if complete
                    else "Bitget UTA open-order result requires another page"
                ),
            )
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
        generation = await self._detect_account_generation()
        if generation == "uta":
            if exchange_order_id is None:
                return _missing_order_id("Bitget UTA")
            payload = await self._get(
                "/api/v3/trade/fills",
                category=_bitget_uta_category(market),
                orderId=exchange_order_id,
                startTime=_milliseconds(since),
                endTime=self.clock_ms(),
                limit=100,
            )
            data = _bitget_result(payload, "UTA fills")
            items = data.get("list") or []
            fills = [
                RemoteFill(
                    exchange_trade_id=str(
                        item.get("execId") or item.get("execLinkId") or ""
                    ),
                    exchange_order_id=str(
                        item.get("orderId") or exchange_order_id
                    ),
                    client_order_id=(
                        str(item["clientOid"])
                        if item.get("clientOid")
                        else client_order_id
                    ),
                    market=market,
                    symbol=str(item.get("symbol") or symbol),
                    side=str(item.get("side") or "").lower(),
                    quantity=Decimal(str(item.get("execQty") or "0")),
                    price=Decimal(str(item.get("execPrice") or "0")),
                    fee_amount=_bitget_uta_fee(item),
                    fee_asset=_bitget_fee_asset(item),
                    liquidity=str(item.get("tradeScope") or "taker").lower(),
                    occurred_at=_from_milliseconds(item.get("createdTime")),
                )
                for item in items
            ]
            return RemoteFillBatch(
                fills=fills,
                complete=len(items) < 100,
                incomplete_reason=(
                    "Bitget UTA order fills require another page"
                    if len(items) >= 100
                    else None
                ),
            )
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
        generation = await self._detect_account_generation()
        if generation == "uta":
            payload = await self._get(
                "/api/v3/trade/order-info",
                clientOid=client_order_id,
            )
            _bitget_success(payload)
            data = payload.get("data")
            return RemoteOrderLookup(
                order=(
                    _bitget_uta_order(data, market=market)
                    if isinstance(data, dict)
                    else None
                ),
                complete=True,
            )
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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        generation = await self._detect_account_generation()
        if generation == "uta":
            payload = await self._get(
                "/api/v3/account/financial-records",
                category="USDT-FUTURES",
                coin="USDT",
                startTime=_milliseconds(since),
                limit=100,
            )
            result = _bitget_result(payload, "funding income")
            items = result.get("list") or []
            funding_types = {
                "CONTRACT_MAIN_SETTLE_FEE_USER_IN",
                "CONTRACT_MAIN_SETTLE_FEE_USER_OUT",
                "MARGIN_SETTLE_FEE_USER_IN",
                "MARGIN_SETTLE_FEE_USER_OUT",
                "FIXED_SETTLE_FEE_USER_IN",
                "FIXED_SETTLE_FEE_USER_OUT",
            }
            records = []
            for item in items:
                record_type = str(item.get("type") or "")
                if record_type not in funding_types:
                    continue
                amount = abs(Decimal(str(item.get("amount") or "0")))
                if record_type.endswith("_OUT"):
                    amount = -amount
                symbol = str(item.get("symbol") or "")
                records.append(
                    RemoteFundingIncome(
                        exchange_record_id=str(item.get("id") or ""),
                        symbol=symbol,
                        base_asset=_funding_base_asset(self.exchange, symbol),
                        asset=str(item.get("coin") or "USDT"),
                        amount=amount,
                        occurred_at=_from_milliseconds(
                            item.get("cTime") or item.get("uTime")
                        ),
                    )
                )
            return _funding_batch(
                records,
                limit_reached=bool(result.get("endId")),
                exchange="Bitget",
            )

        payload = await self._get(
            "/api/v2/mix/account/bill",
            productType="USDT-FUTURES",
            businessType="contract_settle_fee",
            startTime=_milliseconds(since),
            limit=100,
        )
        result = _bitget_result(payload, "funding income")
        items = result.get("bills") or []
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("billId") or ""),
                symbol=str(item.get("symbol") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("symbol") or ""),
                ),
                asset=str(item.get("coin") or ""),
                amount=Decimal(str(item.get("amount") or "0")),
                occurred_at=_from_milliseconds(item.get("cTime")),
            )
            for item in items
        ]
        return _funding_batch(
            records,
            limit_reached=bool(result.get("endId")) and len(items) >= 100,
            exchange="Bitget",
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        generation = await self._detect_account_generation()
        if generation == "uta":
            if not re.fullmatch(r"[.A-Za-z0-9_:/\-]{1,32}", order.client_order_id):
                raise ValueError(
                    "Bitget UTA client order IDs must be at most 32 supported "
                    "ASCII characters"
                )
            values: dict[str, object] = {
                "category": _bitget_uta_category(order.market),
                "symbol": order.symbol,
                "qty": format(order.quantity, "f"),
                "price": format(order.limit_price, "f"),
                "side": order.side,
                "orderType": "limit",
                "timeInForce": "ioc",
                "clientOid": order.client_order_id,
            }
            if order.market == "perp":
                values["marginMode"] = "isolated"
                if order.position_mode == PositionMode.HEDGE:
                    values["posSide"] = "short"
                elif order.reduce_only:
                    values["reduceOnly"] = "yes"
            payload = await self._post("/api/v3/trade/place-order", **values)
            result = _bitget_result(payload, "UTA order submission")
            result_client_id = str(
                result.get("clientOid") or order.client_order_id
            )
            if not result_client_id:
                raise PrivateRequestError(
                    "Bitget UTA order submission returned no client order ID"
                )
            return OrderSubmission(
                market=order.market,
                symbol=order.symbol,
                client_order_id=result_client_id,
                exchange_order_id=(
                    str(result["orderId"]) if result.get("orderId") else None
                ),
            )
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
        generation = await self._detect_account_generation()
        if generation == "uta":
            values: dict[str, object] = {
                "category": _bitget_uta_category(market),
            }
            if exchange_order_id is not None:
                values["orderId"] = exchange_order_id
            else:
                values["clientOid"] = client_order_id or ""
            payload = await self._post("/api/v3/trade/cancel-order", **values)
            result = _bitget_result(payload, "UTA order cancellation")
            result_order_id = (
                str(result["orderId"])
                if result.get("orderId")
                else exchange_order_id
            )
            result_client_id = (
                str(result["clientOid"])
                if result.get("clientOid")
                else client_order_id
            )
            if result_order_id is None and result_client_id is None:
                raise PrivateRequestError(
                    "Bitget UTA order cancellation returned no order identifier"
                )
            return OrderCancellation(
                market=market,
                symbol=symbol,
                client_order_id=result_client_id,
                exchange_order_id=result_order_id,
                accepted=True,
            )
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
        generation = await self._detect_account_generation()
        if generation == "uta":
            return await self._configure_uta_perp(
                symbol=symbol,
                leverage=leverage,
                position_mode=position_mode,
            )
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

    async def _configure_uta_perp(
        self,
        *,
        symbol: str,
        leverage: int,
        position_mode: PositionMode,
    ) -> PerpConfiguration:
        settings = await self._settings(refresh=True)
        detected_mode = _bitget_position_mode(settings.get("holdMode"))
        if detected_mode != position_mode:
            raise PrivateRequestError(
                "Bitget UTA position mode does not match the requested configuration"
            )
        current = _bitget_uta_symbol_configuration(settings, symbol)
        if _bitget_uta_configuration_matches(current, leverage):
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        pending_payload, positions_payload = await _gather(
            self._get(
                "/api/v3/trade/unfilled-orders",
                category="USDT-FUTURES",
                symbol=symbol,
                limit=100,
            ),
            self._get(
                "/api/v3/position/current-position",
                category="USDT-FUTURES",
                symbol=symbol,
            ),
        )
        pending = _bitget_result(pending_payload, "UTA open orders")
        positions = _bitget_result(positions_payload, "UTA positions")
        if pending.get("list") or any(
            Decimal(str(item.get("total") or "0")) != 0
            for item in positions.get("list") or []
        ):
            raise PrivateRequestError(
                "cannot change Bitget UTA leverage with open orders or positions"
            )
        configured = await self._post(
            "/api/v3/account/set-leverage",
            category="USDT-FUTURES",
            symbol=symbol,
            leverage=str(leverage),
            marginMode="isolated",
            posSide="short",
        )
        _bitget_success(configured)
        confirmed_settings = await self._settings(refresh=True)
        confirmed = _bitget_uta_symbol_configuration(confirmed_settings, symbol)
        if not _bitget_uta_configuration_matches(confirmed, leverage):
            raise PrivateRequestError(
                "Bitget UTA isolated leverage was not confirmed by account state"
            )
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        generation = await self._detect_account_generation()
        if generation == "uta":
            raise UnsupportedTradingError(
                "Bitget unified accounts share spot and futures collateral"
            )
        from_type, to_type = _bitget_transfer_accounts(direction)
        payload = await self._post(
            "/api/v2/spot/wallet/transfer",
            fromType=from_type,
            toType=to_type,
            amount=format(amount, "f"),
            coin="USDT",
            clientOid=transfer_id,
        )
        result = _bitget_result(payload, "internal transfer")
        remote_id = str(result.get("transferId") or "")
        returned_client_id = str(result.get("clientOid") or "")
        if not remote_id or returned_client_id != transfer_id:
            raise PrivateRequestError(
                "Bitget internal transfer response identifiers do not match"
            )
        return InternalTransferSubmission(
            transfer_id=remote_id,
            status="pending",
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        generation = await self._detect_account_generation()
        if generation == "uta":
            raise UnsupportedTradingError(
                "Bitget unified accounts share spot and futures collateral"
            )
        from_type, to_type = _bitget_transfer_accounts(direction)
        payload = await self._get(
            "/api/v2/spot/account/transferRecords",
            coin="USDT",
            fromType=from_type,
            startTime=int(_utc_datetime(created_at).timestamp() * 1000),
            endTime=self.clock_ms(),
            clientOid=client_transfer_id,
            limit=100,
        )
        _bitget_success(payload)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PrivateRequestError(
                "Bitget internal transfer history is incomplete"
            )
        match = next(
            (
                item
                for item in rows
                if str(item.get("clientOid") or "") == client_transfer_id
            ),
            None,
        )
        if match is None:
            return RemoteInternalTransfer(
                transfer_id=transfer_id,
                status="unknown",
                direction=direction,
                amount=amount,
            )
        if (
            (transfer_id and str(match.get("transferId") or "") != transfer_id)
            or not str(match.get("transferId") or "")
            or str(match.get("coin") or "").upper() != "USDT"
            or str(match.get("fromType") or "") != from_type
            or str(match.get("toType") or "") != to_type
            or Decimal(str(match.get("size") or "0")) != amount
        ):
            raise PrivateRequestError(
                "Bitget internal transfer history does not match the request"
            )
        raw_status = str(match.get("status") or "").lower()
        status: Literal["pending", "completed", "failed", "unknown"]
        if raw_status == "successful":
            status = "completed"
        elif raw_status == "processing":
            status = "pending"
        elif raw_status == "failed":
            status = "failed"
        else:
            status = "unknown"
        return RemoteInternalTransfer(
            transfer_id=str(match.get("transferId")),
            status=status,
            direction=direction,
            amount=amount,
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
        self.secrets = secrets
        self.environment = environment
        self.clock_s = clock_s or (lambda: int(time.time()))
        self.http = client or httpx.AsyncClient(
            base_url=gate_endpoints(environment).rest,
            timeout=timeout,
        )
        self._owned = client is None
        self._account_mode = "classic"

    async def _get(self, path: str, **params: object) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> Any:
        return await self._request("POST", path, params=params, body=body)

    async def _delete(self, path: str, **params: object) -> Any:
        return await self._request("DELETE", path, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> Any:
        ordered_params = _ordered(params or {})
        query = _query(ordered_params)
        ordered_body = _ordered(body or {})
        content = (
            json.dumps(ordered_body, separators=(",", ":"), ensure_ascii=False)
            if body is not None
            else ""
        )
        timestamp = str(self.clock_s())
        body_hash = hashlib.sha512(content.encode()).hexdigest()
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"{method}\n{path}\n{query}\n{body_hash}\n{timestamp}",
            "sha512",
        )
        return await _json_request(
            self.http,
            method,
            path,
            params=ordered_params,
            content=content or None,
            headers={
                "KEY": self.secrets.api_key,
                "Timestamp": timestamp,
                "SIGN": signature,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Gate-Size-Decimal": "1",
            },
        )

    async def snapshot(self) -> AccountSnapshot:
        spot, perp = await _gather(
            self._get("/api/v4/spot/accounts", currency="USDT"),
            self._get("/api/v4/futures/usdt/accounts"),
        )
        margin_mode = int(perp.get("margin_mode") or 0)
        if margin_mode not in {0, 2}:
            raise PrivateRequestError(
                "Gate unified account mode is not supported; use classic or portfolio"
            )
        try:
            keys = await self._get("/api/v4/account/main_keys")
        except PrivateRequestError:
            trade_permission = None
        else:
            trade_permission = _gate_trade_permission(
                keys,
                self.secrets.api_key,
                require_unified=margin_mode == 2,
            )
        if margin_mode == 2:
            mode, unified = await _gather(
                self._get("/api/v4/unified/unified_mode"),
                self._get("/api/v4/unified/accounts"),
            )
            if not isinstance(mode, dict) or mode.get("mode") != "portfolio":
                raise PrivateRequestError(
                    "Gate portfolio margin mode could not be confirmed"
                )
            if not isinstance(unified, dict):
                raise PrivateRequestError(
                    "Gate unified account balance response is incomplete"
                )
            balances = unified.get("balances")
            usdt = balances.get("USDT") if isinstance(balances, dict) else None
            if not isinstance(usdt, dict):
                raise PrivateRequestError(
                    "Gate unified account did not return a USDT balance"
                )
            self._account_mode = "portfolio"
            return AccountSnapshot(
                exchange=self.exchange,
                environment=self.environment,
                observed_at=datetime.now(UTC),
                spot_usdt_available=_required_non_negative_decimal(
                    usdt.get("available"),
                    "Gate unified USDT available balance",
                ),
                perp_usdt_available=_required_non_negative_decimal(
                    unified.get("total_available_margin"),
                    "Gate unified total available margin",
                ),
                perp_usdt_equity=_required_non_negative_decimal(
                    unified.get("unified_account_total_equity"),
                    "Gate unified account total equity",
                ),
                shared_balance=True,
                account_mode="unified:portfolio",
                position_mode=(
                    PositionMode.HEDGE
                    if perp.get("in_dual_mode") is True
                    else PositionMode.ONE_WAY
                ),
                trade_permission=trade_permission,
                perp_margin_mode=PerpMarginMode.CROSS,
            )
        self._account_mode = "classic"
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
            trade_permission=trade_permission,
            perp_margin_mode=PerpMarginMode.ISOLATED,
        )

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        from_type, to_type = _gate_transfer_accounts(direction)
        payload = await self._post(
            "/api/v4/wallet/transfers",
            body={
                "currency": "USDT",
                "from": from_type,
                "to": to_type,
                "amount": format(amount, "f"),
                "settle": "usdt",
                "client_order_id": transfer_id,
            },
        )
        remote_id = (
            str(payload.get("tx_id") or "")
            if isinstance(payload, dict)
            else ""
        )
        if not remote_id:
            raise PrivateRequestError(
                "Gate internal transfer response is incomplete"
            )
        return InternalTransferSubmission(
            transfer_id=remote_id,
            status="pending",
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        del created_at
        _gate_transfer_accounts(direction)
        query: dict[str, object] = {"client_order_id": client_transfer_id}
        if transfer_id:
            query["tx_id"] = transfer_id
        payload = await self._get("/api/v4/wallet/order_status", **query)
        if not isinstance(payload, dict):
            raise PrivateRequestError(
                "Gate internal transfer lookup is incomplete"
            )
        returned_id = str(payload.get("tx_id") or "")
        if not returned_id or (transfer_id and returned_id != transfer_id):
            raise PrivateRequestError(
                "Gate internal transfer lookup does not match the request"
            )
        raw_status = str(payload.get("status") or "").upper()
        status: Literal["pending", "completed", "failed", "unknown"]
        if raw_status == "SUCCESS":
            status = "completed"
        elif raw_status == "PENDING":
            status = "pending"
        elif raw_status in {"FAIL", "PARTIAL_SUCCESS"}:
            status = "failed"
        else:
            status = "unknown"
        return RemoteInternalTransfer(
            transfer_id=returned_id,
            status=status,
            direction=direction,
            amount=amount,
        )

    async def user_id(self) -> str:
        account = await self._get("/api/v4/futures/usdt/accounts")
        value = str(account.get("user") or "")
        if not value.isdigit() or int(value) <= 0:
            raise PrivateRequestError(
                "Gate futures account did not return a valid user identifier"
            )
        return value

    async def trading_state(self) -> RemoteTradingState:
        spot_account = self._spot_account()
        spot_groups, perp_orders, position_items = await _gather(
            self._get(
                "/api/v4/spot/open_orders",
                page=1,
                limit=100,
                account=spot_account,
            ),
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
            if side == "dual_long":
                side = "long"
            elif side == "dual_short":
                side = "short"
            if side not in {"long", "short"}:
                side = "long" if quantity > 0 else "short"
            margin_mode = str(item.get("pos_margin_mode") or "").lower()
            positions.append(
                RemotePosition(
                    symbol=str(item.get("contract") or ""),
                    side=side,
                    quantity=abs(quantity),
                    entry_price=Decimal(str(item.get("entry_price") or "0")),
                    mark_price=Decimal(str(item.get("mark_price") or "0")),
                    liquidation_price=_optional_decimal(item.get("liq_price")),
                    leverage=(
                        Decimal("1")
                        if self._account_mode == "portfolio"
                        else _gate_position_leverage(item, margin_mode)
                    ),
                    isolated=(
                        False
                        if self._account_mode == "portfolio"
                        else margin_mode == "isolated"
                        if margin_mode
                        else None
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
                account=self._spot_account(),
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
                account=self._spot_account(),
            )
            order = _gate_spot_order(item)
        else:
            item = await self._get(
                f"/api/v4/futures/usdt/orders/{client_order_id}",
            )
            order = _gate_perp_order(item)
        return RemoteOrderLookup(order=order, complete=True)

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        items = await self._request(
            "GET",
            "/api/v4/futures/usdt/account_book",
            params={
                "type": "fund",
                "from": int(since.timestamp()),
                "limit": 100,
                "offset": 0,
            },
        )
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("id") or ""),
                symbol=str(item.get("contract") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("contract") or ""),
                ),
                asset="USDT",
                amount=Decimal(str(item.get("change") or "0")),
                occurred_at=_from_seconds(item.get("time")),
            )
            for item in items
        ]
        return _funding_batch(
            records,
            limit_reached=len(items) >= 100,
            exchange="Gate",
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if (
            not re.fullmatch(r"t-[A-Za-z0-9_.-]+", order.client_order_id)
            or len(order.client_order_id.encode()) > 30
        ):
            raise ValueError(
                "Gate client order IDs must start with t-, use supported ASCII "
                "characters, and contain at most 28 bytes after the prefix"
            )
        if order.market == "spot":
            path = "/api/v4/spot/orders"
            body: dict[str, object] = {
                "text": order.client_order_id,
                "currency_pair": order.symbol,
                "type": "limit",
                "account": self._spot_account(),
                "side": order.side,
                "amount": format(order.quantity, "f"),
                "price": format(order.limit_price, "f"),
                "time_in_force": "ioc",
            }
        else:
            path = "/api/v4/futures/usdt/orders"
            signed_quantity = (
                order.quantity if order.side == "buy" else -order.quantity
            )
            body = {
                "contract": order.symbol,
                "size": format(signed_quantity, "f"),
                "price": format(order.limit_price, "f"),
                "tif": "ioc",
                "text": order.client_order_id,
                "reduce_only": order.reduce_only,
                "pos_margin_mode": (
                    "cross" if self._account_mode == "portfolio" else "isolated"
                ),
                "action_mode": "ACK",
            }
        result = await self._post(path, body=body)
        if not isinstance(result, dict):
            raise PrivateRequestError("Gate order submission returned no result")
        exchange_order_id = str(result.get("id") or "")
        result_client_id = str(result.get("text") or order.client_order_id)
        if not exchange_order_id or not result_client_id:
            raise PrivateRequestError(
                "Gate order submission returned no order identifiers"
            )
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=result_client_id,
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
        target = exchange_order_id or client_order_id or ""
        if market == "spot":
            result = await self._delete(
                f"/api/v4/spot/orders/{target}",
                currency_pair=symbol,
                account=self._spot_account(),
            )
        else:
            result = await self._delete(
                f"/api/v4/futures/usdt/orders/{target}",
            )
        if not isinstance(result, dict):
            raise PrivateRequestError("Gate order cancellation returned no result")
        result_order_id = str(result.get("id") or exchange_order_id or "")
        result_client_id = str(result.get("text") or client_order_id or "")
        if not result_order_id and not result_client_id:
            raise PrivateRequestError(
                "Gate order cancellation returned no order identifier"
            )
        return OrderCancellation(
            market=market,
            symbol=symbol,
            client_order_id=result_client_id or None,
            exchange_order_id=result_order_id or None,
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
        account, positions = await _gather(
            self._get("/api/v4/futures/usdt/accounts"),
            self._get("/api/v4/futures/usdt/positions"),
        )
        detected_mode = (
            PositionMode.HEDGE
            if account.get("in_dual_mode") is True
            else PositionMode.ONE_WAY
        )
        if detected_mode != position_mode:
            raise PrivateRequestError(
                "Gate position mode does not match the requested configuration"
            )
        raw_margin_mode = int(account.get("margin_mode") or 0)
        if raw_margin_mode not in {0, 2}:
            raise PrivateRequestError(
                "Gate unified account mode is not supported; use classic or portfolio"
            )
        portfolio = raw_margin_mode == 2
        if portfolio:
            mode = await self._get("/api/v4/unified/unified_mode")
            if not isinstance(mode, dict) or mode.get("mode") != "portfolio":
                raise PrivateRequestError(
                    "Gate portfolio margin mode could not be confirmed"
                )
            self._account_mode = "portfolio"
            if leverage != 1:
                raise PrivateRequestError(
                    "Gate portfolio margin automation only supports "
                    "conservative 1x accounting"
                )
            return PerpConfiguration(
                symbol=symbol,
                leverage=1,
                isolated=False,
                position_mode=position_mode,
            )
        else:
            self._account_mode = "classic"
        target_mode = "dual_short" if position_mode == PositionMode.HEDGE else "single"
        target = next(
            (
                item
                for item in positions
                if str(item.get("contract") or "") == symbol
                and str(item.get("mode") or "single") == target_mode
            ),
            None,
        )
        target_margin_mode = "isolated"
        current_leverage = _gate_position_leverage(
            target or {},
            str((target or {}).get("pos_margin_mode") or "").lower(),
        )
        isolated = (
            str((target or {}).get("pos_margin_mode") or "").lower() == "isolated"
        )
        if (
            target is not None
            and isolated
            and current_leverage == Decimal(leverage)
        ):
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        open_orders = await self._get(
            "/api/v4/futures/usdt/orders",
            contract=symbol,
            status="open",
            limit=100,
            offset=0,
        )
        has_position = any(
            str(item.get("contract") or "") == symbol
            and Decimal(str(item.get("size") or "0")) != 0
            for item in positions
        )
        if open_orders or has_position:
            raise PrivateRequestError(
                "cannot change Gate margin or leverage with open orders or positions"
            )
        params: dict[str, object] = {
            "leverage": str(leverage),
            "margin_mode": "isolated",
        }
        if position_mode == PositionMode.HEDGE:
            params["dual_side"] = "dual_short"
        configured = await self._post(
            f"/api/v4/futures/usdt/positions/{symbol}/set_leverage",
            params=params,
        )
        if not isinstance(configured, dict):
            raise PrivateRequestError("Gate leverage configuration returned no result")
        confirmed_margin_mode = str(
            configured.get("pos_margin_mode") or ""
        ).lower()
        confirmed_leverage = _gate_position_leverage(
            configured,
            confirmed_margin_mode,
        )
        if (
            confirmed_margin_mode != target_margin_mode
            or confirmed_leverage != Decimal(leverage)
        ):
            raise PrivateRequestError(
                "Gate margin and leverage configuration was not confirmed"
            )
        return PerpConfiguration(
            symbol=symbol,
            leverage=leverage,
            isolated=True,
            position_mode=position_mode,
        )

    def _spot_account(self) -> str:
        return "unified" if self._account_mode == "portfolio" else "spot"

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
        self._configured_leverage: dict[str, int] = {}

    async def _spot_get(self, path: str, **params: object) -> Any:
        return await self._spot_signed("GET", path, **params)

    async def _spot_signed(
        self,
        method: str,
        path: str,
        **params: object,
    ) -> Any:
        values = _ordered({"recvWindow": 5000, "timestamp": self.clock_ms(), **params})
        values["signature"] = _hmac_hex(self.secrets.api_secret, _query(values))
        return await _json_request(
            self.spot,
            method,
            path,
            params=values if method in {"GET", "DELETE"} else None,
            data=values if method == "POST" else None,
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

    async def _perp_post(self, path: str, body: object) -> Any:
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(self.clock_ms())
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"{self.secrets.api_key}{timestamp}{content}",
        )
        return await _json_request(
            self.perp,
            "POST",
            path,
            content=content,
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
            # The spot response explicitly reports canTrade. MEXC documents
            # the successfully queried contract position-mode endpoint as
            # requiring Trading permission, so both legs are confirmed here.
            trade_permission=(
                True
                if spot.get("canTrade") is True
                else False
                if spot.get("canTrade") is False
                else None
            ),
        )

    async def submit_internal_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        amount: Decimal,
    ) -> InternalTransferSubmission:
        del transfer_id
        from_type, to_type = _mexc_transfer_accounts(direction)
        payload = await self._spot_signed(
            "POST",
            "/api/v3/capital/transfer",
            fromAccountType=from_type,
            toAccountType=to_type,
            asset="USDT",
            amount=format(amount, "f"),
        )
        item = (
            payload[0]
            if isinstance(payload, list) and payload
            else payload
        )
        remote_id = (
            str(item.get("tranId") or "")
            if isinstance(item, dict)
            else ""
        )
        if not remote_id:
            raise PrivateRequestError(
                "MEXC internal transfer response is incomplete"
            )
        return InternalTransferSubmission(
            transfer_id=remote_id,
            status="pending",
        )

    async def internal_transfer_status(
        self,
        *,
        transfer_id: str,
        client_transfer_id: str,
        direction: str,
        amount: Decimal,
        created_at: datetime,
    ) -> RemoteInternalTransfer:
        del client_transfer_id, created_at
        from_type, to_type = _mexc_transfer_accounts(direction)
        item = await self._spot_get(
            "/api/v3/capital/transfer/tranId",
            tranId=transfer_id,
        )
        if not isinstance(item, dict):
            raise PrivateRequestError(
                "MEXC internal transfer lookup is incomplete"
            )
        if (
            str(item.get("tranId") or "") != transfer_id
            or str(item.get("asset") or "").upper() != "USDT"
            or str(item.get("fromAccountType") or "") != from_type
            or str(item.get("toAccountType") or "") != to_type
            or Decimal(str(item.get("amount") or "0")) != amount
        ):
            raise PrivateRequestError(
                "MEXC internal transfer history does not match the request"
            )
        raw_status = str(item.get("status") or "").upper()
        status: Literal["pending", "completed", "failed", "unknown"]
        if raw_status == "SUCCESS":
            status = "completed"
        elif raw_status in {"PENDING", "PROCESSING"}:
            status = "pending"
        elif raw_status in {"FAILED", "FAILURE"}:
            status = "failed"
        else:
            status = "unknown"
        return RemoteInternalTransfer(
            transfer_id=transfer_id,
            status=status,
            direction=direction,
            amount=amount,
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

    async def funding_income(
        self,
        *,
        since: datetime,
    ) -> RemoteFundingIncomeBatch:
        payload = await self._perp_get(
            "/api/v1/private/position/funding_records",
            page_num=1,
            page_size=100,
        )
        if not _mexc_success(payload):
            raise PrivateRequestError("MEXC funding income request failed")
        data = payload.get("data") or {}
        items = data.get("resultList") or []
        records = [
            RemoteFundingIncome(
                exchange_record_id=str(item.get("id") or ""),
                symbol=str(item.get("symbol") or ""),
                base_asset=_funding_base_asset(
                    self.exchange,
                    str(item.get("symbol") or ""),
                ),
                asset="USDT",
                amount=Decimal(str(item.get("funding") or "0")),
                rate=_optional_decimal(item.get("rate")),
                position_value=_optional_decimal(item.get("positionValue")),
                occurred_at=_from_milliseconds(item.get("settleTime")),
            )
            for item in items
            if _from_milliseconds(item.get("settleTime")) >= since
        ]
        total_count = int(data.get("totalCount") or len(items))
        return _funding_batch(
            records,
            limit_reached=total_count > len(items),
            exchange="MEXC",
        )

    async def place_limit_ioc(self, order: LimitIocOrder) -> OrderSubmission:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", order.client_order_id):
            raise ValueError(
                "MEXC client order IDs must be at most 32 supported ASCII characters"
            )
        if order.market == "spot":
            item = await self._spot_signed(
                "POST",
                "/api/v3/order",
                symbol=order.symbol,
                side=order.side.upper(),
                type="IMMEDIATE_OR_CANCEL",
                quantity=format(order.quantity, "f"),
                price=format(order.limit_price, "f"),
                newClientOrderId=order.client_order_id,
            )
            if not isinstance(item, dict):
                raise PrivateRequestError("MEXC spot order returned no result")
            exchange_order_id = str(item.get("orderId") or "")
            if not exchange_order_id:
                raise PrivateRequestError("MEXC spot order returned no order ID")
            return OrderSubmission(
                market=order.market,
                symbol=order.symbol,
                client_order_id=str(
                    item.get("clientOrderId") or order.client_order_id
                ),
                exchange_order_id=exchange_order_id,
            )
        leverage = self._configured_leverage.get(order.symbol)
        if leverage is None:
            raise PrivateRequestError(
                "MEXC contract write capability and isolated leverage must be "
                "confirmed before order submission"
            )
        payload = await self._perp_post(
            "/api/v1/private/order/submit",
            {
                "symbol": order.symbol,
                "price": format(order.limit_price, "f"),
                "vol": format(order.quantity, "f"),
                "leverage": leverage,
                "side": 2 if order.reduce_only else 3,
                "type": 3,
                "openType": 1,
                "externalOid": order.client_order_id,
                "positionMode": (
                    1 if order.position_mode == PositionMode.HEDGE else 2
                ),
                **(
                    {"reduceOnly": True}
                    if order.position_mode == PositionMode.ONE_WAY
                    and order.reduce_only
                    else {}
                ),
            },
        )
        if not _mexc_success(payload) or payload.get("data") in (None, ""):
            self._configured_leverage.pop(order.symbol, None)
            raise PrivateRequestError(
                "MEXC contract order submission was rejected; trading is read-only"
            )
        return OrderSubmission(
            market=order.market,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=str(payload["data"]),
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
        if market == "spot":
            params: dict[str, object] = {"symbol": symbol}
            if exchange_order_id is not None:
                params["orderId"] = exchange_order_id
            else:
                params["origClientOrderId"] = client_order_id or ""
            item = await self._spot_signed("DELETE", "/api/v3/order", **params)
            if not isinstance(item, dict):
                raise PrivateRequestError(
                    "MEXC spot order cancellation returned no result"
                )
            return OrderCancellation(
                market=market,
                symbol=symbol,
                client_order_id=str(
                    item.get("origClientOrderId")
                    or item.get("clientOrderId")
                    or client_order_id
                    or ""
                )
                or None,
                exchange_order_id=str(item.get("orderId") or exchange_order_id or "")
                or None,
                accepted=True,
            )
        if exchange_order_id is not None:
            payload = await self._perp_post(
                "/api/v1/private/order/cancel",
                [exchange_order_id],
            )
            items = payload.get("data") or [] if isinstance(payload, dict) else []
            accepted = (
                _mexc_success(payload)
                and bool(items)
                and int(items[0].get("errorCode") or 0) == 0
            )
        else:
            payload = await self._perp_post(
                "/api/v1/private/order/cancel_with_external",
                {"symbol": symbol, "externalOid": client_order_id},
            )
            accepted = _mexc_success(payload)
        if not accepted:
            raise PrivateRequestError("MEXC contract order cancellation was rejected")
        return OrderCancellation(
            market=market,
            symbol=symbol,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
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
        mode, leverage_payload, pending_payload, positions_payload = await _gather(
            self._perp_get("/api/v1/private/position/position_mode"),
            self._perp_get("/api/v1/private/position/leverage", symbol=symbol),
            self._perp_get(
                "/api/v1/private/order/list/open_orders",
                symbol=symbol,
                page_num=1,
                page_size=100,
            ),
            self._perp_get("/api/v1/private/position/open_positions", symbol=symbol),
        )
        for payload in (
            mode,
            leverage_payload,
            pending_payload,
            positions_payload,
        ):
            if not _mexc_success(payload):
                raise PrivateRequestError(
                    "MEXC contract capability probe failed; trading is read-only"
                )
        detected_mode = (
            PositionMode.HEDGE
            if mode.get("data") == 1
            else PositionMode.ONE_WAY
            if mode.get("data") == 2
            else PositionMode.UNKNOWN
        )
        if detected_mode != position_mode:
            raise PrivateRequestError(
                "MEXC position mode does not match the requested configuration"
            )
        position_items = positions_payload.get("data") or []
        target_position = next(
            (
                item
                for item in position_items
                if str(item.get("symbol") or "") == symbol
                and int(item.get("positionType") or 0) == 2
            ),
            None,
        )
        leverage_items = leverage_payload.get("data") or []
        if isinstance(leverage_items, dict):
            leverage_items = [leverage_items]
        target_leverage = next(
            (
                item
                for item in leverage_items
                if int(item.get("positionType") or 0) == 2
            ),
            None,
        )
        if (
            target_position is not None
            and int(target_position.get("openType") or 0) == 1
            and Decimal(str(target_position.get("leverage") or "0"))
            == Decimal(leverage)
            and target_leverage is not None
            and Decimal(str(target_leverage.get("leverage") or "0"))
            == Decimal(leverage)
        ):
            self._configured_leverage[symbol] = leverage
            return PerpConfiguration(
                symbol=symbol,
                leverage=leverage,
                isolated=True,
                position_mode=position_mode,
            )
        pending_data = pending_payload.get("data") or {}
        open_orders = (
            pending_data.get("resultList")
            if isinstance(pending_data, dict)
            else pending_data
        ) or []
        if open_orders or any(
            Decimal(str(item.get("holdVol") or "0")) != 0
            for item in position_items
        ):
            raise PrivateRequestError(
                "cannot change MEXC leverage with open orders or positions"
            )
        configured = await self._perp_post(
            "/api/v1/private/position/change_leverage",
            {
                "openType": 1,
                "leverage": leverage,
                "symbol": symbol,
                "positionType": 2,
            },
        )
        if not _mexc_success(configured):
            self._configured_leverage.pop(symbol, None)
            raise PrivateRequestError(
                "MEXC contract write capability probe failed; trading is read-only"
            )
        confirmed = await self._perp_get(
            "/api/v1/private/position/leverage",
            symbol=symbol,
        )
        if not _mexc_success(confirmed):
            self._configured_leverage.pop(symbol, None)
            raise PrivateRequestError(
                "MEXC leverage configuration could not be confirmed"
            )
        confirmed_items = confirmed.get("data") or []
        if isinstance(confirmed_items, dict):
            confirmed_items = [confirmed_items]
        short = next(
            (
                item
                for item in confirmed_items
                if int(item.get("positionType") or 0) == 2
            ),
            None,
        )
        if short is None or Decimal(
            str(short.get("leverage") or "0")
        ) != Decimal(leverage):
            self._configured_leverage.pop(symbol, None)
            raise PrivateRequestError(
                "MEXC isolated leverage configuration was not confirmed"
            )
        self._configured_leverage[symbol] = leverage
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


def _bitget_position_mode(value: object) -> PositionMode:
    return (
        PositionMode.HEDGE
        if value == "hedge_mode"
        else PositionMode.ONE_WAY
        if value == "one_way_mode"
        else PositionMode.UNKNOWN
    )


def _bitget_transfer_accounts(direction: str) -> tuple[str, str]:
    if direction == "spot_to_perp":
        return "spot", "usdt_futures"
    if direction == "perp_to_spot":
        return "usdt_futures", "spot"
    raise ValueError("unsupported internal transfer direction")


def _bitget_uta_category(market: str) -> str:
    return "SPOT" if market == "spot" else "USDT-FUTURES"


def _bitget_uta_symbol_configuration(
    settings: dict[str, Any],
    symbol: str,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in settings.get("symbolConfigList") or []
            if str(item.get("category") or "").upper() == "USDT-FUTURES"
            and str(item.get("symbol") or "") == symbol
        ),
        None,
    )


def _bitget_uta_configuration_matches(
    configuration: dict[str, Any] | None,
    leverage: int,
) -> bool:
    if configuration is None:
        return False
    raw_leverage = configuration.get("leverage")
    if isinstance(raw_leverage, list):
        values = {
            Decimal(str(item))
            for item in raw_leverage
            if item not in (None, "")
        }
        leverage_matches = Decimal(leverage) in values
    else:
        leverage_matches = Decimal(
            str(raw_leverage or "0")
        ) == Decimal(leverage)
    return (
        str(configuration.get("marginMode") or "").lower() == "isolated"
        and leverage_matches
    )


def _bitget_trade_permission(
    info: dict[str, Any],
    *,
    generation: Literal["classic", "uta"],
) -> bool | None:
    if generation == "uta":
        permission_type = str(info.get("permType") or "").lower()
        permissions = {
            str(item).lower()
            for item in (info.get("permissions") or [])
            if item
        }
        if not permission_type or not permissions:
            return None
        return (
            permission_type == "read-and-write"
            and {"uta_trade", "uta_mgt"}.issubset(permissions)
        )
    authorities = {
        str(item).lower()
        for item in (info.get("authorities") or [])
        if item
    }
    if not authorities:
        return None
    return {"stow", "coow", "cpow"}.issubset(authorities)


def _gate_trade_permission(
    payload: Any,
    api_key: str,
    *,
    require_unified: bool = False,
) -> bool | None:
    if not isinstance(payload, list):
        return None
    matching = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if key == api_key or _masked_key_matches(key, api_key):
            matching.append(item)
    if len(matching) != 1:
        return None
    current = matching[0]
    if int(current.get("state") or 0) != 1:
        return False
    if current.get("currency_pairs"):
        # A global ready flag cannot prove that every scanner symbol is in a
        # per-key pair whitelist. Keep it unknown until pair-scoped capability
        # checks are modeled.
        return None
    permissions = {
        str(item.get("name") or "").lower(): item.get("read_only")
        for item in (current.get("perms") or [])
        if isinstance(item, dict)
    }
    if not permissions:
        return None
    required = {"spot", "futures"}
    if require_unified:
        required.add("unified")
    return all(permissions.get(name) is False for name in required)


def _masked_key_matches(masked: str, value: str) -> bool:
    if "*" not in masked:
        return False
    prefix, _, remainder = masked.partition("*")
    suffix = remainder.lstrip("*")
    return (
        bool(prefix or suffix)
        and value.startswith(prefix)
        and value.endswith(suffix)
    )


def _mexc_success(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("success") is True
        and int(payload.get("code") or 0) == 0
    )


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _required_non_negative_decimal(value: object, label: str) -> Decimal:
    if value in (None, ""):
        raise PrivateRequestError(f"{label} is missing")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise PrivateRequestError(f"{label} is invalid")
    return parsed


def _gate_position_leverage(
    item: dict[str, Any],
    margin_mode: str,
) -> Decimal:
    if margin_mode == "cross":
        value = item.get("cross_leverage_limit")
        if value not in (None, ""):
            return Decimal(str(value))
    return Decimal(str(item.get("lever") or item.get("leverage") or "0"))


def _milliseconds(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _from_milliseconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(value or 0) / 1000, tz=UTC)


def _from_seconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(value or 0), tz=UTC)


def _funding_base_asset(exchange: Exchange, symbol: str) -> str:
    value = symbol.upper()
    if exchange == Exchange.OKX and value.endswith("-USDT-SWAP"):
        value = value[: -len("-USDT-SWAP")]
    elif exchange in {Exchange.GATE, Exchange.MEXC} and value.endswith("_USDT"):
        value = value[: -len("_USDT")]
    elif value.endswith("USDT"):
        value = value[:-4].rstrip("-_")
    else:
        value = ""
    if not value:
        raise PrivateRequestError("funding income returned an invalid USDT symbol")
    return value


def _funding_batch(
    records: list[RemoteFundingIncome],
    *,
    limit_reached: bool,
    exchange: str,
) -> RemoteFundingIncomeBatch:
    for record in records:
        if not record.exchange_record_id:
            raise PrivateRequestError(
                f"{exchange} funding income returned no record ID"
            )
        if record.asset.upper() != "USDT":
            raise PrivateRequestError(
                f"{exchange} funding income returned a non-USDT asset"
            )
    return RemoteFundingIncomeBatch(
        records=records,
        complete=not limit_reached,
        incomplete_reason=(
            None
            if not limit_reached
            else f"{exchange} funding income result requires another page"
        ),
    )


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


def _bitget_uta_fee(item: dict[str, Any]) -> Decimal:
    details = _bitget_fee_details(item)
    return sum(
        (
            abs(Decimal(str(detail.get("fee") or "0")))
            for detail in details
        ),
        Decimal("0"),
    )


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


def _bitget_uta_order(item: dict[str, Any], *, market: str) -> RemoteOrder:
    return RemoteOrder(
        exchange_order_id=str(item.get("orderId") or ""),
        client_order_id=str(item["clientOid"]) if item.get("clientOid") else None,
        market=market,
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or "").lower(),
        status=str(item.get("orderStatus") or ""),
        price=Decimal(str(item.get("avgPrice") or item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("qty") or "0")),
        filled_quantity=Decimal(str(item.get("cumExecQty") or "0")),
        reduce_only=(
            str(item.get("reduceOnly") or "").lower() == "yes"
            or (
                market == "perp"
                and str(item.get("posSide") or "").lower() == "short"
                and str(item.get("side") or "").lower() == "buy"
            )
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
