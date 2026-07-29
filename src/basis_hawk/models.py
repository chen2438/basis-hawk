from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class Exchange(StrEnum):
    BINANCE = "binance"
    OKX = "okx"
    MEXC = "mexc"
    BYBIT = "bybit"
    BITGET = "bitget"
    GATE = "gate"


class Quality(StrEnum):
    HEALTHY = "healthy"
    WARMING = "warming"
    STALE = "stale"


class DecimalModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_serializer("*", when_used="json")
    def serialize_decimal(self, value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        return value


class InstrumentPair(DecimalModel):
    exchange: Exchange
    base_asset: str
    quote_asset: str = "USDT"
    spot_symbol: str
    perp_symbol: str
    funding_interval_hours: Decimal = Decimal("8")
    spot_price_increment: Decimal = Field(default=Decimal("0"), ge=0)
    spot_quantity_increment: Decimal = Field(default=Decimal("0"), ge=0)
    spot_min_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    spot_min_notional: Decimal = Field(default=Decimal("0"), ge=0)
    perp_price_increment: Decimal = Field(default=Decimal("0"), ge=0)
    perp_quantity_increment: Decimal = Field(default=Decimal("0"), ge=0)
    perp_min_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    perp_min_notional: Decimal = Field(default=Decimal("0"), ge=0)
    perp_contract_size: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def key(self) -> str:
        return f"{self.exchange.value}:{self.base_asset}"

    @property
    def trading_rules_complete(self) -> bool:
        return all(
            value > 0
            for value in (
                self.spot_price_increment,
                self.spot_quantity_increment,
                self.perp_price_increment,
                self.perp_quantity_increment,
                self.perp_contract_size,
            )
        )

    @property
    def perp_base_quantity_increment(self) -> Decimal:
        return self.perp_quantity_increment * self.perp_contract_size


class MarketQuote(DecimalModel):
    exchange: Exchange
    base_asset: str
    observed_at: datetime
    spot_bid: Decimal
    spot_bid_qty: Decimal
    spot_ask: Decimal
    spot_ask_qty: Decimal
    perp_bid: Decimal
    perp_bid_qty: Decimal
    perp_ask: Decimal
    perp_ask_qty: Decimal
    spot_quote_volume_24h: Decimal
    perp_quote_volume_24h: Decimal


class FundingObservation(DecimalModel):
    exchange: Exchange
    base_asset: str
    rate: Decimal
    funding_at: datetime
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    settled: bool = False
    next_funding_at: datetime | None = None
    interval_hours: Decimal = Decimal("8")


class FeeRate(DecimalModel):
    spot_taker: Decimal
    perp_taker: Decimal


DEFAULT_FEES: dict[Exchange, FeeRate] = {
    Exchange.BINANCE: FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.0005")),
    Exchange.OKX: FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.0005")),
    Exchange.MEXC: FeeRate(spot_taker=Decimal("0.0005"), perp_taker=Decimal("0.0004")),
    Exchange.BYBIT: FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.00055")),
    Exchange.BITGET: FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.0006")),
    Exchange.GATE: FeeRate(spot_taker=Decimal("0.001"), perp_taker=Decimal("0.00075")),
}


class ScannerSettings(BaseModel):
    universe_size: int = Field(default=500, ge=10, le=500)
    minimum_quote_volume: Decimal = Field(default=Decimal("1000000"), ge=0)
    holding_period_days: int = Field(default=30, ge=1, le=365)
    retention_days: int = Field(default=7, ge=1, le=7)
    fees: dict[Exchange, FeeRate] = Field(default_factory=lambda: dict(DEFAULT_FEES))
    fee_checked_at: str = "2026-07-23"

    @model_validator(mode="before")
    @classmethod
    def fill_new_exchange_fees(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            **value,
            "fees": {
                **DEFAULT_FEES,
                **(value.get("fees") or {}),
            },
        }

    @field_serializer("minimum_quote_volume", when_used="json")
    def serialize_minimum_volume(self, value: Decimal) -> str:
        return format(value, "f")


class Opportunity(DecimalModel):
    exchange: Exchange
    base_asset: str
    spot_symbol: str
    perp_symbol: str
    observed_at: datetime
    spot_bid: Decimal
    spot_ask: Decimal
    perp_bid: Decimal
    perp_ask: Decimal
    executable_basis: Decimal
    top_book_notional: Decimal
    close_top_book_notional: Decimal
    current_funding_rate: Decimal
    funding_interval_hours: Decimal
    next_funding_at: datetime | None
    current_apr: Decimal
    apr_24h: Decimal | None
    apr_7d: Decimal | None
    net_return: Decimal | None
    spot_quote_volume_24h: Decimal
    perp_quote_volume_24h: Decimal
    spot_taker_fee: Decimal
    perp_taker_fee: Decimal
    quality: Quality
    spot_ask_notional: Decimal = Decimal("0")
    perp_bid_notional: Decimal = Decimal("0")

    @property
    def key(self) -> str:
        return f"{self.exchange.value}:{self.base_asset}"


class ExchangeStatus(BaseModel):
    exchange: Exchange
    state: str = "starting"
    last_catalog_at: datetime | None = None
    last_quote_at: datetime | None = None
    last_funding_at: datetime | None = None
    latency_ms: int | None = None
    error: str | None = None
    instruments: int = 0
    history_ready: int = 0
    history_progress_percent: float = 0.0
    history_download_rate_per_minute: float | None = None
    history_syncing: bool = False
