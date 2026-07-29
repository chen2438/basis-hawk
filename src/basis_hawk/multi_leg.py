from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class ExecutionEnvironment(StrEnum):
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE = "live"


class QuantityMode(StrEnum):
    BASE = "base"
    USDT = "usdt"


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


class LegRole(StrEnum):
    ANCHOR = "anchor"
    HEDGE = "hedge"


class LegSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderMode(StrEnum):
    MAKER = "maker"
    PROTECTED_IOC = "protected_ioc"
    MARKET = "market"


class MarginMode(StrEnum):
    ISOLATED = "isolated"
    CROSS = "cross"


class HedgeTriggerMode(StrEnum):
    REALTIME = "realtime"
    CUMULATIVE_PERCENT = "cumulative_percent"


class ExecutionTaskStatus(StrEnum):
    DRAFT = "draft"
    PREFLIGHT_READY = "preflight_ready"
    QUEUED = "queued"
    RUNNING = "running"
    HEDGING = "hedging"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"
    EMERGENCY_STOPPED = "emergency_stopped"


class ExecutionRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class ExecutionOrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class StrategyStatus(StrEnum):
    RUNNING = "running"
    CLOSING = "closing"
    ENDED = "ended"
    MANUAL_REVIEW = "manual_review"


class DecimalPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_serializer("*", when_used="json")
    def serialize_decimal(self, value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        return value


class MakerPolicy(DecimalPayload):
    book_level: int = Field(default=3, ge=1, le=20)
    maximum_chases: int = Field(default=50, ge=0, le=200)
    fallback_mode: OrderMode | None = OrderMode.PROTECTED_IOC

    @model_validator(mode="after")
    def validate_fallback(self) -> MakerPolicy:
        if self.fallback_mode == OrderMode.MAKER:
            raise ValueError("maker fallback mode cannot be maker")
        return self


class ExecutionTaskLegSpec(DecimalPayload):
    account_id: str | None = None
    role: LegRole
    market_type: MarketType
    side: LegSide
    base_asset: str = Field(min_length=1, max_length=40)
    quote_asset: str = "USDT"
    symbol: str = Field(min_length=1, max_length=100)
    target_quantity: Decimal = Field(gt=0)
    per_order_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    order_mode: OrderMode
    maximum_slippage: Decimal = Field(default=Decimal("0.001"), gt=0, le=Decimal("0.25"))
    maker_policy: MakerPolicy | None = None
    margin_mode: MarginMode | None = None
    leverage: int = Field(default=1, ge=1, le=10)
    reduce_only: bool = False

    @model_validator(mode="after")
    def validate_market_and_order_mode(self) -> ExecutionTaskLegSpec:
        if self.quote_asset.upper() != "USDT":
            raise ValueError("multi-leg tasks only support USDT quote or settlement")
        if self.per_order_quantity > self.target_quantity:
            raise ValueError("per-order quantity cannot exceed target quantity")
        if self.order_mode == OrderMode.MAKER and self.maker_policy is None:
            raise ValueError("maker legs require a maker policy")
        if self.order_mode != OrderMode.MAKER and self.maker_policy is not None:
            raise ValueError("only maker legs may define a maker policy")
        if self.market_type == MarketType.SPOT:
            if self.margin_mode is not None:
                raise ValueError("spot legs cannot define a margin mode")
            if self.leverage != 1:
                raise ValueError("spot legs must use 1x leverage")
            if self.reduce_only:
                raise ValueError("spot inventory sells are reserved instead of reduce-only")
        elif self.margin_mode is None:
            raise ValueError("perpetual legs require a margin mode")
        return self

    @property
    def signed_target(self) -> Decimal:
        sign = Decimal("1") if self.side == LegSide.BUY else Decimal("-1")
        return sign * self.target_quantity


class ExecutionTaskSpec(DecimalPayload):
    name: str = Field(min_length=1, max_length=160)
    display_symbol: str = Field(min_length=1, max_length=100)
    environment: ExecutionEnvironment
    base_asset: str = Field(min_length=1, max_length=40)
    quantity_mode: QuantityMode
    legs: list[ExecutionTaskLegSpec] = Field(min_length=2, max_length=16)
    hedge_trigger: HedgeTriggerMode = HedgeTriggerMode.REALTIME
    hedge_threshold: Decimal | None = Field(default=None, gt=0, le=1)
    maximum_base_exposure: Decimal = Field(gt=0)
    maximum_notional_exposure_usdt: Decimal = Field(gt=0)
    maximum_retries: int = Field(default=3, ge=0, le=20)
    create_strategy: bool = True
    source_opportunity_id: str | None = None

    @model_validator(mode="after")
    def validate_task(self) -> ExecutionTaskSpec:
        normalized_base = self.base_asset.upper()
        if any(leg.base_asset.upper() != normalized_base for leg in self.legs):
            raise ValueError("all task legs must use the task base asset")
        if sum(leg.role == LegRole.ANCHOR for leg in self.legs) != 1:
            raise ValueError("a task must contain exactly one anchor leg")
        if self.hedge_trigger == HedgeTriggerMode.CUMULATIVE_PERCENT:
            if self.hedge_threshold is None:
                raise ValueError("cumulative hedging requires a threshold")
        elif self.hedge_threshold is not None:
            raise ValueError("realtime hedging cannot define a cumulative threshold")
        if self.quantity_mode == QuantityMode.BASE:
            target_delta = abs(sum((leg.signed_target for leg in self.legs), Decimal("0")))
            if target_delta > self.maximum_base_exposure:
                raise ValueError("planned base delta exceeds the task exposure limit")
        if self.environment != ExecutionEnvironment.PAPER and any(
            not leg.account_id for leg in self.legs
        ):
            raise ValueError("sandbox and live legs require an account")
        return self

    @property
    def anchor(self) -> ExecutionTaskLegSpec:
        return next(leg for leg in self.legs if leg.role == LegRole.ANCHOR)

