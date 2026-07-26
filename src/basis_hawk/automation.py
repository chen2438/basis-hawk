from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from basis_hawk.models import Exchange


class AutoStrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: Literal["sandbox", "live"]
    enabled_exchanges: set[Exchange] = Field(min_length=1)
    leverage: int = Field(ge=1, le=10)
    notional_per_trade: Decimal = Field(gt=0)
    per_exchange_max_exposure: Decimal = Field(gt=0)
    global_max_exposure: Decimal = Field(gt=0)
    max_concurrent_positions: int = Field(ge=1, le=1000)
    minimum_current_apr: Decimal
    minimum_apr_24h: Decimal
    minimum_apr_7d: Decimal
    minimum_net_return: Decimal
    maximum_opening_basis: Decimal = Field(gt=Decimal("-1"), lt=Decimal("1"))
    minimum_two_leg_notional: Decimal = Field(gt=0)
    book_capacity_multiple: Decimal = Field(ge=1, le=100)
    normal_max_slippage: Decimal = Field(gt=0, le=Decimal("0.1"))
    emergency_max_slippage: Decimal = Field(gt=0, le=Decimal("0.25"))
    daily_max_loss: Decimal = Field(gt=0)
    minimum_reentry_minutes: int = Field(ge=0, le=10080)
    maximum_holding_hours: int = Field(ge=1, le=8760)
    minimum_liquidation_buffer: Decimal = Field(gt=0, le=Decimal("1"))
    close_funding_rate_below: Decimal
    close_net_return_below: Decimal
    close_basis_above: Decimal = Field(gt=Decimal("-1"), lt=Decimal("1"))
    take_profit_usdt: Decimal = Field(gt=0)
    stop_loss_usdt: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_portfolio_limits(self) -> AutoStrategyConfig:
        if self.per_exchange_max_exposure < self.notional_per_trade:
            raise ValueError(
                "per-exchange exposure must cover at least one trade"
            )
        if self.global_max_exposure < self.per_exchange_max_exposure:
            raise ValueError(
                "global exposure must be at least the per-exchange limit"
            )
        if self.minimum_two_leg_notional > self.notional_per_trade:
            raise ValueError(
                "minimum two-leg notional cannot exceed trade notional"
            )
        if self.emergency_max_slippage < self.normal_max_slippage:
            raise ValueError(
                "emergency slippage cannot be below normal slippage"
            )
        return self

    @field_serializer("*", when_used="json")
    def serialize_values(self, value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, set):
            return sorted(
                item.value if isinstance(item, Exchange) else str(item)
                for item in value
            )
        return value
