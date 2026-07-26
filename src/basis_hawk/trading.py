from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer

from basis_hawk.models import Exchange, Opportunity, Quality, ScannerSettings
from basis_hawk.storage import Database, OrderLegRow, TradeIntentRow


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
    idempotency_key: str
    exchange: Exchange
    environment: str
    base_asset: str
    action: str
    status: TradeIntentStatus
    requested_notional: Decimal
    base_quantity: Decimal
    market_observed_at: datetime
    config_version: str
    version: int
    created_at: datetime
    updated_at: datetime
    legs: list[OrderLegView]

    @field_serializer("requested_notional", "base_quantity", when_used="json")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


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
        config_version = hashlib.sha256(
            settings.model_dump_json().encode()
        ).hexdigest()
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

    async def get(self, intent_id: str) -> TradeIntentView | None:
        value = await self.database.trade_intent(intent_id)
        return _view(*value) if value is not None else None


def _view(row: TradeIntentRow, legs: list[OrderLegRow]) -> TradeIntentView:
    return TradeIntentView(
        id=row.id,
        idempotency_key=row.idempotency_key,
        exchange=Exchange(row.exchange),
        environment=row.environment,
        base_asset=row.base_asset,
        action=row.action,
        status=TradeIntentStatus(row.status),
        requested_notional=row.requested_notional,
        base_quantity=row.base_quantity,
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
            for item in legs
        ],
    )
