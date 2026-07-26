from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, field_serializer

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
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        response = await client.request(method, path, params=params, headers=headers)
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
        values = _ordered({"recvWindow": 5000, "timestamp": self.clock_ms(), **params})
        values["signature"] = _hmac_hex(self.secrets.api_secret, _query(values))
        return await _json_request(
            client,
            "GET",
            path,
            params=values,
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
                reduce_only=bool(item.get("reduceOnly")),
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
        timestamp = self.clock().astimezone(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        headers = {
            "OK-ACCESS-KEY": self.secrets.api_key,
            "OK-ACCESS-SIGN": _hmac_base64(
                self.secrets.api_secret,
                f"{timestamp}GET{request_path}",
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.secrets.passphrase or "",
        }
        if self.environment == ExchangeEnvironment.SANDBOX:
            headers["x-simulated-trading"] = "1"
        return await _json_request(self.http, "GET", path, params=params, headers=headers)

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
                reduce_only=str(item.get("reduceOnly") or "false").lower() == "true",
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
        timestamp = str(self.clock_ms())
        recv_window = "5000"
        signature = _hmac_hex(
            self.secrets.api_secret,
            f"{timestamp}{self.secrets.api_key}{recv_window}{query}",
        )
        return await _json_request(
            self.http,
            "GET",
            path,
            params=params,
            headers={
                "X-BAPI-API-KEY": self.secrets.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
            },
        )

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
        wallet, info = await _gather(
            self._get(
                "/v5/account/wallet-balance",
                accountType="UNIFIED",
                coin="USDT",
            ),
            self._get("/v5/account/info"),
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
            position_mode=PositionMode.UNKNOWN,
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
        timestamp = str(self.clock_ms())
        headers = {
            "ACCESS-KEY": self.secrets.api_key,
            "ACCESS-SIGN": _hmac_base64(
                self.secrets.api_secret,
                f"{timestamp}GET{request_path}",
            ),
            "ACCESS-PASSPHRASE": self.secrets.passphrase or "",
            "ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if self.environment == ExchangeEnvironment.SANDBOX:
            headers["paptrading"] = "1"
        return await _json_request(self.http, "GET", path, params=params, headers=headers)

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


def _bybit_success(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise PrivateRequestError("Bybit private account request was rejected")


def _bitget_success(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("code") != "00000":
        raise PrivateRequestError("Bitget private account request was rejected")


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


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
        side=str(item.get("side") or "").lower(),
        status=str(item.get("status") or ""),
        price=Decimal(str(item.get("priceAvg") or item.get("price") or "0")),
        original_quantity=Decimal(str(item.get("size") or "0")),
        filled_quantity=Decimal(
            str(item.get("baseVolume") or item.get("filledQty") or "0")
        ),
        reduce_only=str(item.get("reduceOnly") or "").lower() == "yes",
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
        reduce_only=bool(item.get("reduce_only")),
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
