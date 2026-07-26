from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import (
    Exchange,
    InstrumentPair,
    Opportunity,
    Quality,
    ScannerSettings,
)
from basis_hawk.sizing import (
    OrderSizingError,
    protective_limit_price,
    size_paired_order,
)
from basis_hawk.storage import (
    Database,
    FillRow,
    OrderLegRow,
    PairedPositionRow,
    TradeIntentRow,
)


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


class TradeIntentStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    HEDGED = "hedged"
    CLOSING = "closing"
    COMPENSATING = "compensating"
    MANUAL_REVIEW = "manual_review"
    CLOSED = "closed"
    FAILED = "failed"


class OrderLegStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TradeValidationError(ValueError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class StateConflict(RuntimeError):
    pass


class OrderLegView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    leg: str
    market: str
    symbol: str
    side: str
    client_order_id: str
    exchange_order_id: str | None
    status: OrderLegStatus
    quantity: Decimal
    base_multiplier: Decimal
    limit_price: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    reduce_only: bool

    @field_serializer(
        "quantity",
        "base_multiplier",
        "limit_price",
        "filled_quantity",
        "average_price",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class TradeIntentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    paired_position_id: str | None
    idempotency_key: str
    exchange: Exchange
    environment: str
    base_asset: str
    action: str
    status: TradeIntentStatus
    leverage: int
    requested_notional: Decimal
    base_quantity: Decimal
    spot_fee_rate: Decimal
    perp_fee_rate: Decimal
    market_observed_at: datetime
    config_version: str
    version: int
    created_at: datetime
    updated_at: datetime
    legs: list[OrderLegView]

    @field_serializer(
        "requested_notional",
        "base_quantity",
        "spot_fee_rate",
        "perp_fee_rate",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class FillView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    exchange_trade_id: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_asset: str
    liquidity: str
    occurred_at: datetime

    @field_serializer("quantity", "price", "fee_amount", when_used="json")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class PairedPositionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    opening_intent_id: str
    closing_intent_id: str | None
    exchange: Exchange
    environment: str
    base_asset: str
    initial_quantity: Decimal
    quantity: Decimal
    spot_entry_price: Decimal
    perp_entry_price: Decimal
    opening_fees_usdt: Decimal
    remaining_opening_fees_usdt: Decimal
    closing_fees_usdt: Decimal | None
    realized_pnl_usdt: Decimal | None
    status: str
    opened_at: datetime
    closed_at: datetime | None

    @field_serializer(
        "initial_quantity",
        "quantity",
        "spot_entry_price",
        "perp_entry_price",
        "opening_fees_usdt",
        "remaining_opening_fees_usdt",
        "closing_fees_usdt",
        "realized_pnl_usdt",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class LiveOpenPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_fingerprint: str = Field(exclude=True)
    config_version: str = Field(exclude=True)
    exchange: Exchange
    environment: ExchangeEnvironment
    base_asset: str
    leverage: int
    requested_notional: Decimal
    maximum_slippage: Decimal
    market_observed_at: datetime
    expires_at: datetime
    spot_symbol: str
    spot_reference_price: Decimal
    spot_limit_price: Decimal
    spot_quantity: Decimal
    spot_usdt_required: Decimal
    perp_symbol: str
    perp_reference_price: Decimal
    perp_limit_price: Decimal
    perp_quantity: Decimal
    perp_base_multiplier: Decimal
    perp_usdt_margin_required: Decimal
    base_quantity: Decimal
    estimated_total_fees_usdt: Decimal
    worst_case_basis: Decimal

    @field_serializer(
        "requested_notional",
        "maximum_slippage",
        "spot_reference_price",
        "spot_limit_price",
        "spot_quantity",
        "spot_usdt_required",
        "perp_reference_price",
        "perp_limit_price",
        "perp_quantity",
        "perp_base_multiplier",
        "perp_usdt_margin_required",
        "base_quantity",
        "estimated_total_fees_usdt",
        "worst_case_basis",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class PaperExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    executed: int
    compensated: int
    manual_review: int


TRANSITIONS: dict[TradeIntentStatus, set[TradeIntentStatus]] = {
    TradeIntentStatus.PLANNED: {
        TradeIntentStatus.EXECUTING,
        TradeIntentStatus.FAILED,
    },
    TradeIntentStatus.EXECUTING: {
        TradeIntentStatus.HEDGED,
        TradeIntentStatus.COMPENSATING,
        TradeIntentStatus.MANUAL_REVIEW,
        TradeIntentStatus.FAILED,
    },
    TradeIntentStatus.HEDGED: {
        TradeIntentStatus.CLOSING,
        TradeIntentStatus.MANUAL_REVIEW,
    },
    TradeIntentStatus.CLOSING: {
        TradeIntentStatus.CLOSED,
        TradeIntentStatus.COMPENSATING,
        TradeIntentStatus.MANUAL_REVIEW,
    },
    TradeIntentStatus.COMPENSATING: {
        TradeIntentStatus.HEDGED,
        TradeIntentStatus.CLOSED,
        TradeIntentStatus.MANUAL_REVIEW,
        TradeIntentStatus.FAILED,
    },
    TradeIntentStatus.MANUAL_REVIEW: {
        TradeIntentStatus.COMPENSATING,
        TradeIntentStatus.HEDGED,
        TradeIntentStatus.CLOSED,
        TradeIntentStatus.FAILED,
    },
    TradeIntentStatus.CLOSED: set(),
    TradeIntentStatus.FAILED: set(),
}


class TradeLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def preview_live_open(
        self,
        *,
        opportunity: Opportunity,
        pair: InstrumentPair,
        notional_usdt: Decimal,
        settings: ScannerSettings,
        environment: ExchangeEnvironment,
        leverage: int = 1,
        maximum_slippage: Decimal = Decimal("0.001"),
        now: datetime | None = None,
    ) -> LiveOpenPreview:
        if leverage < 1 or leverage > 10:
            raise TradeValidationError("leverage must be between 1 and 10")
        if maximum_slippage <= 0 or maximum_slippage > Decimal("0.1"):
            raise TradeValidationError(
                "maximum slippage must be above 0 and at most 0.1"
            )
        if (
            environment == ExchangeEnvironment.SANDBOX
            and opportunity.exchange in {Exchange.MEXC, Exchange.GATE}
        ):
            raise TradeValidationError(
                f"{opportunity.exchange.value} does not provide a supported sandbox"
            )
        if (
            pair.exchange != opportunity.exchange
            or pair.base_asset != opportunity.base_asset
            or pair.spot_symbol != opportunity.spot_symbol
            or pair.perp_symbol != opportunity.perp_symbol
        ):
            raise TradeValidationError(
                "instrument rules do not match the selected opportunity"
            )
        observed_now = now or datetime.now(UTC)
        if opportunity.quality != Quality.HEALTHY:
            raise TradeValidationError("only healthy opportunities can be planned")
        if opportunity.observed_at > observed_now + timedelta(seconds=5):
            raise TradeValidationError("market quote timestamp is in the future")
        if observed_now - opportunity.observed_at > timedelta(seconds=15):
            raise TradeValidationError("market quote is stale")
        if notional_usdt <= 0:
            raise TradeValidationError("notional must be positive")
        if notional_usdt > opportunity.top_book_notional:
            raise TradeValidationError("notional exceeds current top-book capacity")
        try:
            sizing = size_paired_order(
                pair,
                requested_notional=notional_usdt,
                spot_price=opportunity.spot_ask,
                perp_price=opportunity.perp_bid,
            )
            spot_limit = protective_limit_price(
                reference_price=opportunity.spot_ask,
                maximum_slippage=maximum_slippage,
                side="buy",
                price_increment=pair.spot_price_increment,
            )
            perp_limit = protective_limit_price(
                reference_price=opportunity.perp_bid,
                maximum_slippage=maximum_slippage,
                side="sell",
                price_increment=pair.perp_price_increment,
            )
        except OrderSizingError as exc:
            raise TradeValidationError(str(exc)) from exc
        fees = settings.fees[opportunity.exchange]
        spot_notional = sizing.spot_quantity * spot_limit
        perp_notional = sizing.base_quantity * perp_limit
        spot_fee = spot_notional * fees.spot_taker
        perp_fee = perp_notional * fees.perp_taker
        config_version = hashlib.sha256(
            json.dumps(
                {
                    "scanner": settings.model_dump(mode="json"),
                    "environment": environment.value,
                    "leverage": leverage,
                    "maximum_slippage": _canonical_decimal(
                        maximum_slippage
                    ),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "exchange": opportunity.exchange.value,
                    "environment": environment.value,
                    "base_asset": opportunity.base_asset,
                    "notional_usdt": _canonical_decimal(notional_usdt),
                    "leverage": leverage,
                    "maximum_slippage": _canonical_decimal(
                        maximum_slippage
                    ),
                    "spot_symbol": pair.spot_symbol,
                    "perp_symbol": pair.perp_symbol,
                    "spot_quantity": _canonical_decimal(
                        sizing.spot_quantity
                    ),
                    "perp_quantity": _canonical_decimal(
                        sizing.perp_quantity
                    ),
                    "perp_contract_size": _canonical_decimal(
                        pair.perp_contract_size
                    ),
                    "spot_limit": _canonical_decimal(spot_limit),
                    "perp_limit": _canonical_decimal(perp_limit),
                    "market_observed_at": opportunity.observed_at.isoformat(),
                    "config_version": config_version,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return LiveOpenPreview(
            request_fingerprint=fingerprint,
            config_version=config_version,
            exchange=opportunity.exchange,
            environment=environment,
            base_asset=opportunity.base_asset,
            leverage=leverage,
            requested_notional=notional_usdt,
            maximum_slippage=maximum_slippage,
            market_observed_at=opportunity.observed_at,
            expires_at=opportunity.observed_at + timedelta(seconds=15),
            spot_symbol=pair.spot_symbol,
            spot_reference_price=opportunity.spot_ask,
            spot_limit_price=spot_limit,
            spot_quantity=sizing.spot_quantity,
            spot_usdt_required=spot_notional + spot_fee,
            perp_symbol=pair.perp_symbol,
            perp_reference_price=opportunity.perp_bid,
            perp_limit_price=perp_limit,
            perp_quantity=sizing.perp_quantity,
            perp_base_multiplier=pair.perp_contract_size,
            perp_usdt_margin_required=(
                perp_notional / Decimal(leverage) + perp_fee
            ),
            base_quantity=sizing.base_quantity,
            estimated_total_fees_usdt=spot_fee + perp_fee,
            worst_case_basis=(perp_limit - spot_limit) / spot_limit,
        )

    async def plan_paper_open(
        self,
        *,
        opportunity: Opportunity,
        notional_usdt: Decimal,
        idempotency_key: uuid.UUID,
        settings: ScannerSettings,
        now: datetime | None = None,
    ) -> tuple[TradeIntentView, bool]:
        existing = await self.database.trade_intent_by_idempotency(str(idempotency_key))
        if existing is not None:
            row, legs = existing
            if (
                row.environment == "paper"
                and row.action == "open"
                and row.exchange == opportunity.exchange.value
                and row.base_asset == opportunity.base_asset
                and row.requested_notional == notional_usdt
            ):
                return _view(row, legs), False
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        observed_now = now or datetime.now(UTC)
        if opportunity.quality != Quality.HEALTHY:
            raise TradeValidationError("only healthy opportunities can be planned")
        if opportunity.observed_at > observed_now + timedelta(seconds=5):
            raise TradeValidationError("market quote timestamp is in the future")
        if observed_now - opportunity.observed_at > timedelta(seconds=15):
            raise TradeValidationError("market quote is stale")
        if notional_usdt <= 0:
            raise TradeValidationError("notional must be positive")
        if notional_usdt > opportunity.top_book_notional:
            raise TradeValidationError("notional exceeds current top-book capacity")
        if opportunity.spot_ask <= 0 or opportunity.perp_bid <= 0:
            raise TradeValidationError("market prices must be positive")

        quantity = notional_usdt / opportunity.spot_ask
        fees = settings.fees[opportunity.exchange]
        config_version = hashlib.sha256(settings.model_dump_json().encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "exchange": opportunity.exchange.value,
                    "base_asset": opportunity.base_asset,
                    "notional_usdt": format(notional_usdt, "f"),
                    "spot_symbol": opportunity.spot_symbol,
                    "perp_symbol": opportunity.perp_symbol,
                    "spot_price": format(opportunity.spot_ask, "f"),
                    "perp_price": format(opportunity.perp_bid, "f"),
                    "market_observed_at": opportunity.observed_at.isoformat(),
                    "config_version": config_version,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        intent_id = str(uuid.uuid4())
        now_value = datetime.now(UTC)
        client_prefix = f"bh-{intent_id.replace('-', '')[:20]}"
        row, legs, created = await self.database.create_trade_intent(
            intent={
                "id": intent_id,
                "idempotency_key": str(idempotency_key),
                "request_fingerprint": fingerprint,
                "exchange": opportunity.exchange.value,
                "environment": "paper",
                "base_asset": opportunity.base_asset,
                "action": "open",
                "status": TradeIntentStatus.PLANNED.value,
                "requested_notional": notional_usdt,
                "base_quantity": quantity,
                "spot_fee_rate": fees.spot_taker,
                "perp_fee_rate": fees.perp_taker,
                "market_observed_at": opportunity.observed_at,
                "config_version": config_version,
                "version": 1,
                "created_at": now_value,
                "updated_at": now_value,
            },
            legs=[
                {
                    "id": str(uuid.uuid4()),
                    "trade_intent_id": intent_id,
                    "leg": "spot",
                    "market": "spot",
                    "symbol": opportunity.spot_symbol,
                    "side": "buy",
                    "client_order_id": f"{client_prefix}-s",
                    "status": OrderLegStatus.CREATED.value,
                    "quantity": quantity,
                    "limit_price": opportunity.spot_ask,
                    "filled_quantity": Decimal("0"),
                    "reduce_only": False,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
                {
                    "id": str(uuid.uuid4()),
                    "trade_intent_id": intent_id,
                    "leg": "perp",
                    "market": "perp",
                    "symbol": opportunity.perp_symbol,
                    "side": "sell",
                    "client_order_id": f"{client_prefix}-p",
                    "status": OrderLegStatus.CREATED.value,
                    "quantity": quantity,
                    "limit_price": opportunity.perp_bid,
                    "filled_quantity": Decimal("0"),
                    "reduce_only": False,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
            ],
        )
        if row.request_fingerprint != fingerprint:
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        return _view(row, legs), created

    async def transition(
        self,
        *,
        intent_id: str,
        expected_version: int,
        target: TradeIntentStatus,
    ) -> TradeIntentView:
        current = await self.database.trade_intent(intent_id)
        if current is None:
            raise KeyError(intent_id)
        row, legs = current
        status = TradeIntentStatus(row.status)
        if target not in TRANSITIONS[status]:
            raise StateConflict(f"cannot transition trade intent from {status} to {target}")
        updated = await self.database.transition_trade_intent(
            intent_id=intent_id,
            expected_version=expected_version,
            status=target.value,
        )
        if updated is None:
            raise StateConflict("trade intent version changed")
        return _view(updated, legs)

    async def plan_live_open(
        self,
        *,
        opportunity: Opportunity,
        pair: InstrumentPair,
        notional_usdt: Decimal,
        idempotency_key: uuid.UUID,
        settings: ScannerSettings,
        environment: ExchangeEnvironment,
        leverage: int = 1,
        maximum_slippage: Decimal = Decimal("0.001"),
        now: datetime | None = None,
    ) -> tuple[TradeIntentView, bool]:
        existing = await self.database.trade_intent_by_idempotency(
            str(idempotency_key)
        )
        if existing is not None:
            row, legs = existing
            if (
                row.environment == environment.value
                and row.action == "open"
                and row.exchange == opportunity.exchange.value
                and row.base_asset == opportunity.base_asset
                and row.requested_notional == notional_usdt
                and row.leverage == leverage
            ):
                return _view(row, legs), False
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        preview = self.preview_live_open(
            opportunity=opportunity,
            pair=pair,
            notional_usdt=notional_usdt,
            settings=settings,
            environment=environment,
            leverage=leverage,
            maximum_slippage=maximum_slippage,
            now=now,
        )
        fees = settings.fees[opportunity.exchange]
        intent_id = str(uuid.uuid4())
        spot_client_id, perp_client_id = _live_client_order_ids(
            opportunity.exchange,
            intent_id,
        )
        now_value = datetime.now(UTC)
        row, legs, created = await self.database.create_trade_intent(
            intent={
                "id": intent_id,
                "idempotency_key": str(idempotency_key),
                "request_fingerprint": preview.request_fingerprint,
                "exchange": opportunity.exchange.value,
                "environment": environment.value,
                "base_asset": opportunity.base_asset,
                "action": "open",
                "status": TradeIntentStatus.PLANNED.value,
                "leverage": leverage,
                "requested_notional": notional_usdt,
                "base_quantity": preview.base_quantity,
                "spot_fee_rate": fees.spot_taker,
                "perp_fee_rate": fees.perp_taker,
                "market_observed_at": opportunity.observed_at,
                "config_version": preview.config_version,
                "version": 1,
                "created_at": now_value,
                "updated_at": now_value,
            },
            legs=[
                {
                    "id": str(uuid.uuid4()),
                    "trade_intent_id": intent_id,
                    "leg": "spot",
                    "market": "spot",
                    "symbol": pair.spot_symbol,
                    "side": "buy",
                    "client_order_id": spot_client_id,
                    "status": OrderLegStatus.CREATED.value,
                    "quantity": preview.spot_quantity,
                    "base_multiplier": Decimal("1"),
                    "limit_price": preview.spot_limit_price,
                    "filled_quantity": Decimal("0"),
                    "reduce_only": False,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
                {
                    "id": str(uuid.uuid4()),
                    "trade_intent_id": intent_id,
                    "leg": "perp",
                    "market": "perp",
                    "symbol": pair.perp_symbol,
                    "side": "sell",
                    "client_order_id": perp_client_id,
                    "status": OrderLegStatus.CREATED.value,
                    "quantity": preview.perp_quantity,
                    "base_multiplier": preview.perp_base_multiplier,
                    "limit_price": preview.perp_limit_price,
                    "filled_quantity": Decimal("0"),
                    "reduce_only": False,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
            ],
        )
        if row.request_fingerprint != preview.request_fingerprint:
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        return _view(row, legs), created

    async def plan_paper_close(
        self,
        *,
        position_id: str,
        opportunity: Opportunity,
        idempotency_key: uuid.UUID,
        settings: ScannerSettings,
        now: datetime | None = None,
    ) -> tuple[TradeIntentView, bool]:
        existing = await self.database.trade_intent_by_idempotency(str(idempotency_key))
        if existing is not None:
            row, legs = existing
            if (
                row.environment == "paper"
                and row.action == "close"
                and row.paired_position_id == position_id
            ):
                return _view(row, legs), False
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        observed_now = now or datetime.now(UTC)
        position = await self.database.paired_position(position_id)
        if position is None:
            raise TradeValidationError("paired position was not found")
        if position.environment != "paper" or position.status != "open":
            raise TradeValidationError("paired position is not open for paper closing")
        if (
            position.exchange != opportunity.exchange.value
            or position.base_asset != opportunity.base_asset
        ):
            raise TradeValidationError("opportunity does not match paired position")
        if opportunity.quality != Quality.HEALTHY:
            raise TradeValidationError("only healthy opportunities can be closed normally")
        if opportunity.observed_at > observed_now + timedelta(seconds=5):
            raise TradeValidationError("market quote timestamp is in the future")
        if observed_now - opportunity.observed_at > timedelta(seconds=15):
            raise TradeValidationError("market quote is stale")
        if opportunity.spot_bid <= 0 or opportunity.perp_ask <= 0:
            raise TradeValidationError("closing market prices must be positive")
        required_capacity = max(
            position.quantity * opportunity.spot_bid,
            position.quantity * opportunity.perp_ask,
        )
        if required_capacity > opportunity.close_top_book_notional:
            raise TradeValidationError("position exceeds current closing top-book capacity")

        fees = settings.fees[opportunity.exchange]
        config_version = hashlib.sha256(settings.model_dump_json().encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "position_id": position.id,
                    "exchange": opportunity.exchange.value,
                    "base_asset": opportunity.base_asset,
                    "quantity": format(position.quantity, "f"),
                    "spot_price": format(opportunity.spot_bid, "f"),
                    "perp_price": format(opportunity.perp_ask, "f"),
                    "market_observed_at": opportunity.observed_at.isoformat(),
                    "config_version": config_version,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        intent_id = str(uuid.uuid4())
        now_value = datetime.now(UTC)
        client_prefix = f"bh-{intent_id.replace('-', '')[:20]}"
        try:
            row, legs, created = await self.database.create_paper_close_intent(
                position_id=position.id,
                intent={
                    "id": intent_id,
                    "paired_position_id": position.id,
                    "idempotency_key": str(idempotency_key),
                    "request_fingerprint": fingerprint,
                    "exchange": opportunity.exchange.value,
                    "environment": "paper",
                    "base_asset": opportunity.base_asset,
                    "action": "close",
                    "status": TradeIntentStatus.PLANNED.value,
                    "requested_notional": position.quantity * opportunity.spot_bid,
                    "base_quantity": position.quantity,
                    "spot_fee_rate": fees.spot_taker,
                    "perp_fee_rate": fees.perp_taker,
                    "market_observed_at": opportunity.observed_at,
                    "config_version": config_version,
                    "version": 1,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
                legs=[
                    {
                        "id": str(uuid.uuid4()),
                        "trade_intent_id": intent_id,
                        "leg": "spot",
                        "market": "spot",
                        "symbol": opportunity.spot_symbol,
                        "side": "sell",
                        "client_order_id": f"{client_prefix}-s",
                        "status": OrderLegStatus.CREATED.value,
                        "quantity": position.quantity,
                        "limit_price": opportunity.spot_bid,
                        "filled_quantity": Decimal("0"),
                        "reduce_only": False,
                        "created_at": now_value,
                        "updated_at": now_value,
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "trade_intent_id": intent_id,
                        "leg": "perp",
                        "market": "perp",
                        "symbol": opportunity.perp_symbol,
                        "side": "buy",
                        "client_order_id": f"{client_prefix}-p",
                        "status": OrderLegStatus.CREATED.value,
                        "quantity": position.quantity,
                        "limit_price": opportunity.perp_ask,
                        "filled_quantity": Decimal("0"),
                        "reduce_only": True,
                        "created_at": now_value,
                        "updated_at": now_value,
                    },
                ],
            )
        except ValueError as exc:
            raise TradeValidationError(str(exc)) from exc
        if row.request_fingerprint != fingerprint:
            raise IdempotencyConflict(
                "idempotency key was already used for a different trade request"
            )
        return _view(row, legs), created

    async def get(self, intent_id: str) -> TradeIntentView | None:
        value = await self.database.trade_intent(intent_id)
        return _view(*value) if value is not None else None

    async def positions(self, *, status: str | None = None) -> list[PairedPositionView]:
        return [
            _position_view(item)
            for item in await self.database.list_paired_positions(status=status)
        ]

    async def position(self, position_id: str) -> PairedPositionView | None:
        row = await self.database.paired_position(position_id)
        return _position_view(row) if row is not None else None

    async def fills(self, intent_id: str) -> list[FillView]:
        return [_fill_view(item) for item in await self.database.fills_for_intent(intent_id)]


class PaperExecutionService:
    def __init__(
        self,
        database: Database,
        *,
        fill_ratios: dict[str, Decimal] | None = None,
        compensation_succeeds: bool = True,
    ) -> None:
        self.database = database
        self.fill_ratios = fill_ratios or {
            "spot": Decimal("1"),
            "perp": Decimal("1"),
        }
        if set(self.fill_ratios) != {"spot", "perp"} or any(
            value < 0 or value > 1 for value in self.fill_ratios.values()
        ):
            raise ValueError("paper fill ratios must contain spot/perp values from 0 to 1")
        self.compensation_succeeds = compensation_succeeds

    async def run_once(self) -> PaperExecutionResult:
        recoverable = await self.database.recoverable_trade_intents()
        candidates = [
            item
            for item in recoverable
            if item.environment == "paper"
            and item.status
            in {
                TradeIntentStatus.PLANNED.value,
                TradeIntentStatus.COMPENSATING.value,
            }
        ]
        executed = 0
        compensated = 0
        manual_review = 0
        for item in candidates:
            result = None
            if item.status == TradeIntentStatus.COMPENSATING.value:
                result = (
                    await self.database.execute_paper_compensation(
                        intent_id=item.id,
                        succeeds=self.compensation_succeeds,
                    )
                    if item.action == "open"
                    else await self.database.execute_paper_close_compensation(
                        intent_id=item.id,
                        succeeds=self.compensation_succeeds,
                    )
                    if item.action == "close"
                    else None
                )
                if result is not None and result[2]:
                    compensated += int(
                        result[0].status
                        in {
                            TradeIntentStatus.HEDGED.value,
                            TradeIntentStatus.CLOSED.value,
                            TradeIntentStatus.FAILED.value,
                        }
                    )
                    manual_review += int(result[0].status == TradeIntentStatus.MANUAL_REVIEW.value)
            elif item.action == "open":
                current = await self.database.trade_intent(item.id)
                if current is not None:
                    primary = {leg.leg: leg for leg in current[1] if leg.leg in {"spot", "perp"}}
                    if set(primary) == {"spot", "perp"}:
                        result = await self.database.record_paper_open_fills(
                            intent_id=item.id,
                            spot_fill_quantity=(
                                primary["spot"].quantity * self.fill_ratios["spot"]
                            ),
                            perp_fill_quantity=(
                                primary["perp"].quantity * self.fill_ratios["perp"]
                            ),
                        )
                        if (
                            result is not None
                            and result[0].status == TradeIntentStatus.COMPENSATING.value
                        ):
                            result = await self.database.execute_paper_compensation(
                                intent_id=item.id,
                                succeeds=self.compensation_succeeds,
                            )
                            if result is not None and result[2]:
                                compensated += int(
                                    result[0].status
                                    in {
                                        TradeIntentStatus.HEDGED.value,
                                        TradeIntentStatus.FAILED.value,
                                    }
                                )
                                manual_review += int(
                                    result[0].status == TradeIntentStatus.MANUAL_REVIEW.value
                                )
            elif item.action == "close":
                current = await self.database.trade_intent(item.id)
                if current is not None:
                    primary = {
                        leg.leg: leg
                        for leg in current[1]
                        if leg.leg in {"spot", "perp"}
                    }
                    if set(primary) == {"spot", "perp"}:
                        result = await self.database.record_paper_close_fills(
                            intent_id=item.id,
                            spot_fill_quantity=(
                                primary["spot"].quantity
                                * self.fill_ratios["spot"]
                            ),
                            perp_fill_quantity=(
                                primary["perp"].quantity
                                * self.fill_ratios["perp"]
                            ),
                        )
                        if (
                            result is not None
                            and result[0].status
                            == TradeIntentStatus.COMPENSATING.value
                        ):
                            result = (
                                await self.database.execute_paper_close_compensation(
                                    intent_id=item.id,
                                    succeeds=self.compensation_succeeds,
                                )
                            )
                            if result is not None and result[2]:
                                compensated += int(
                                    result[0].status
                                    in {
                                        TradeIntentStatus.CLOSED.value,
                                        TradeIntentStatus.FAILED.value,
                                    }
                                )
                                manual_review += int(
                                    result[0].status
                                    == TradeIntentStatus.MANUAL_REVIEW.value
                                )
            if result is not None and result[2]:
                executed += 1
        return PaperExecutionResult(
            examined=len(candidates),
            executed=executed,
            compensated=compensated,
            manual_review=manual_review,
        )


def _live_client_order_ids(
    exchange: Exchange,
    intent_id: str,
) -> tuple[str, str]:
    token = intent_id.replace("-", "")[:24]
    if exchange == Exchange.OKX:
        return f"bh{token}s", f"bh{token}p"
    if exchange == Exchange.GATE:
        return f"t-bh-{token[:20]}-s", f"t-bh-{token[:20]}-p"
    return f"bh-{token}-s", f"bh-{token}-p"


def _view(row: TradeIntentRow, legs: list[OrderLegRow]) -> TradeIntentView:
    return TradeIntentView(
        id=row.id,
        paired_position_id=row.paired_position_id,
        idempotency_key=row.idempotency_key,
        exchange=Exchange(row.exchange),
        environment=row.environment,
        base_asset=row.base_asset,
        action=row.action,
        status=TradeIntentStatus(row.status),
        leverage=row.leverage,
        requested_notional=row.requested_notional,
        base_quantity=row.base_quantity,
        spot_fee_rate=row.spot_fee_rate,
        perp_fee_rate=row.perp_fee_rate,
        market_observed_at=row.market_observed_at,
        config_version=row.config_version,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        legs=[
            OrderLegView(
                id=item.id,
                leg=item.leg,
                market=item.market,
                symbol=item.symbol,
                side=item.side,
                client_order_id=item.client_order_id,
                exchange_order_id=item.exchange_order_id,
                status=OrderLegStatus(item.status),
                quantity=item.quantity,
                base_multiplier=item.base_multiplier,
                limit_price=item.limit_price,
                filled_quantity=item.filled_quantity,
                average_price=item.average_price,
                reduce_only=item.reduce_only,
            )
            for item in sorted(legs, key=lambda value: value.leg, reverse=True)
        ],
    )


def _fill_view(row: FillRow) -> FillView:
    return FillView(
        id=row.id,
        exchange_trade_id=row.exchange_trade_id,
        quantity=row.quantity,
        price=row.price,
        fee_amount=row.fee_amount,
        fee_asset=row.fee_asset,
        liquidity=row.liquidity,
        occurred_at=row.occurred_at,
    )


def _position_view(row: PairedPositionRow) -> PairedPositionView:
    return PairedPositionView(
        id=row.id,
        opening_intent_id=row.opening_intent_id,
        closing_intent_id=row.closing_intent_id,
        exchange=Exchange(row.exchange),
        environment=row.environment,
        base_asset=row.base_asset,
        initial_quantity=row.initial_quantity,
        quantity=row.quantity,
        spot_entry_price=row.spot_entry_price,
        perp_entry_price=row.perp_entry_price,
        opening_fees_usdt=row.opening_fees_usdt,
        remaining_opening_fees_usdt=row.remaining_opening_fees_usdt,
        closing_fees_usdt=row.closing_fees_usdt,
        realized_pnl_usdt=row.realized_pnl_usdt,
        status=row.status,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
    )
