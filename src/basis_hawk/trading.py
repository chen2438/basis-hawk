from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer

from basis_hawk.models import Exchange, Opportunity, Quality, ScannerSettings
from basis_hawk.storage import (
    Database,
    FillRow,
    OrderLegRow,
    PairedPositionRow,
    TradeIntentRow,
)


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
    limit_price: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    reduce_only: bool

    @field_serializer(
        "quantity",
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
    quantity: Decimal
    spot_entry_price: Decimal
    perp_entry_price: Decimal
    opening_fees_usdt: Decimal
    closing_fees_usdt: Decimal | None
    realized_pnl_usdt: Decimal | None
    status: str
    opened_at: datetime
    closed_at: datetime | None

    @field_serializer(
        "quantity",
        "spot_entry_price",
        "perp_entry_price",
        "opening_fees_usdt",
        "closing_fees_usdt",
        "realized_pnl_usdt",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class PaperExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    executed: int


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
    def __init__(self, database: Database) -> None:
        self.database = database

    async def run_once(self) -> PaperExecutionResult:
        recoverable = await self.database.recoverable_trade_intents()
        candidates = [
            item
            for item in recoverable
            if item.environment == "paper" and item.status == TradeIntentStatus.PLANNED.value
        ]
        executed = 0
        for item in candidates:
            result = (
                await self.database.execute_paper_open(intent_id=item.id)
                if item.action == "open"
                else await self.database.execute_paper_close(intent_id=item.id)
                if item.action == "close"
                else None
            )
            if result is not None and result[2]:
                executed += 1
        return PaperExecutionResult(examined=len(candidates), executed=executed)


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
        quantity=row.quantity,
        spot_entry_price=row.spot_entry_price,
        perp_entry_price=row.perp_entry_price,
        opening_fees_usdt=row.opening_fees_usdt,
        closing_fees_usdt=row.closing_fees_usdt,
        realized_pnl_usdt=row.realized_pnl_usdt,
        status=row.status,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
    )
