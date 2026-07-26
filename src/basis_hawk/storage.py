from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from basis_hawk.models import FundingObservation, InstrumentPair, Opportunity, ScannerSettings

if TYPE_CHECKING:
    from basis_hawk.accounts import OrderSubmission, RemoteFill, RemoteOrder


class Base(DeclarativeBase):
    pass


class InstrumentRow(Base):
    __tablename__ = "instruments"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    base_asset: Mapped[str] = mapped_column(String(40))
    spot_symbol: Mapped[str] = mapped_column(String(80))
    perp_symbol: Mapped[str] = mapped_column(String(80))
    interval_hours: Mapped[str] = mapped_column(String(32))
    spot_price_increment: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    spot_quantity_increment: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    spot_min_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    spot_min_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_price_increment: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_quantity_increment: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_min_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_min_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_contract_size: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundingRow(Base):
    __tablename__ = "funding_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(20))
    base_asset: Mapped[str] = mapped_column(String(40))
    funding_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rate: Mapped[str] = mapped_column(String(48))
    interval_hours: Mapped[str] = mapped_column(String(32))
    __table_args__ = (Index("uq_funding", "exchange", "base_asset", "funding_at", unique=True),)


class SnapshotRow(Base):
    __tablename__ = "opportunity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(20))
    base_asset: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_snapshot_history", "exchange", "base_asset", "observed_at"),)


class LatestOpportunityRow(Base):
    __tablename__ = "latest_opportunities"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    base_asset: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class AdminUserRow(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_ciphertext: Mapped[str] = mapped_column(Text)
    totp_nonce: Mapped[str] = mapped_column(String(80))
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminSessionRow(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ExchangeCredentialRow(Base):
    __tablename__ = "exchange_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20))
    environment: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(100))
    masked_api_key: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(String(80))
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("exchange", "environment", name="uq_exchange_credential"),)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text)


class NotificationOutboxRow(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    channel: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    __table_args__ = (
        UniqueConstraint(
            "dedupe_key",
            "channel",
            name="uq_notification_outbox_dedupe_channel",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_notification_outbox_severity",
        ),
        CheckConstraint(
            "channel IN ('telegram', 'email')",
            name="ck_notification_outbox_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'retry', 'sent', 'dead')",
            name="ck_notification_outbox_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_notification_outbox_attempts",
        ),
        Index(
            "ix_notification_outbox_claim",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )


class NotificationProjectionStateRow(Base):
    __tablename__ = "notification_projection_state"
    source_key: Mapped[str] = mapped_column(String(150), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "generation >= 0",
            name="ck_notification_projection_generation",
        ),
    )


@dataclass(frozen=True)
class NotificationOutboxItem:
    id: str
    dedupe_key: str
    event_type: str
    severity: str
    channel: str
    subject: str
    body: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


@dataclass(frozen=True)
class DailyNotificationSummary:
    period_start: datetime
    period_end: datetime
    realized_event_count: int
    realized_net_pnl_usdt: Decimal
    opened_trade_count: int
    closed_trade_count: int
    failed_trade_count: int
    active_position_count: int
    unhealthy_account_count: int


class InternalTransferRow(Base):
    __tablename__ = "internal_transfers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    environment: Mapped[str] = mapped_column(String(20))
    asset: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    status: Mapped[str] = mapped_column(String(30), index=True)
    exchange_transfer_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    source_balance_before: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    target_balance_before: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    expected_target_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    __table_args__ = (
        CheckConstraint(
            "asset = 'USDT'",
            name="ck_internal_transfer_usdt_only",
        ),
        CheckConstraint(
            "direction IN ('spot_to_perp', 'perp_to_spot')",
            name="ck_internal_transfer_direction",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_internal_transfer_amount_positive",
        ),
        CheckConstraint(
            "status IN "
            "('planned', 'submitted', 'pending', 'completed', 'failed', "
            "'manual_review')",
            name="ck_internal_transfer_status",
        ),
        Index(
            "ix_internal_transfer_daily_limit",
            "created_at",
            "status",
        ),
    )


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20))
    environment: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    spot_usdt_available: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_usdt_available: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_usdt_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    shared_balance: Mapped[bool] = mapped_column(Boolean)
    account_mode: Mapped[str] = mapped_column(String(100))
    position_mode: Mapped[str] = mapped_column(String(20))
    trade_permission: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    __table_args__ = (
        Index(
            "ix_account_snapshot_history",
            "exchange",
            "environment",
            "observed_at",
        ),
    )


class AccountReconciliationRow(Base):
    __tablename__ = "account_reconciliation"
    exchange: Mapped[str] = mapped_column(String(20), primary_key=True)
    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(300))
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    trading_state_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    fill_reconciliation_complete: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    order_reconciliation_complete: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    private_stream_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    open_order_count: Mapped[int] = mapped_column(Integer, default=0)
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, default=0)
    recovered_order_count: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PrivateStreamStateRow(Base):
    __tablename__ = "private_stream_states"
    exchange: Mapped[str] = mapped_column(String(20), primary_key=True)
    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    orders_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    fills_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    positions_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RemoteOpenOrderSnapshotRow(Base):
    __tablename__ = "remote_open_order_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("account_snapshots.id", ondelete="CASCADE"),
        index=True,
    )
    exchange_order_id: Mapped[str] = mapped_column(String(100))
    client_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    market: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(50))
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    original_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    reduce_only: Mapped[bool] = mapped_column(Boolean)


class RemotePositionSnapshotRow(Base):
    __tablename__ = "remote_position_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("account_snapshots.id", ondelete="CASCADE"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    mark_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    liquidation_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    leverage: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    isolated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class ExecutionControlRow(Base):
    __tablename__ = "execution_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyVersionRow(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    environment: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "environment IN ('sandbox', 'live')",
            name="ck_strategy_version_environment",
        ),
    )


class AutomationControlRow(Base):
    __tablename__ = "automation_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(20))
    active_strategy_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(String(300))
    updated_by: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "state IN ('disabled', 'enabled', 'paused')",
            name="ck_automation_control_state",
        ),
    )


class TradePreviewRow(Base):
    __tablename__ = "trade_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(20), default="open")
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    paired_position_id: Mapped[str | None] = mapped_column(
        ForeignKey("paired_positions.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    environment: Mapped[str] = mapped_column(String(20))
    base_asset: Mapped[str] = mapped_column(String(40))
    requested_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    leverage: Mapped[int] = mapped_column(Integer)
    maximum_slippage: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    market_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmation_idempotency_key: Mapped[str | None] = mapped_column(
        String(36),
        unique=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    __table_args__ = (
        CheckConstraint(
            "action IN ('open', 'close')",
            name="ck_trade_preview_action",
        ),
        CheckConstraint(
            "(action = 'open' AND paired_position_id IS NULL) OR "
            "(action = 'close' AND paired_position_id IS NOT NULL)",
            name="ck_trade_preview_position_action",
        ),
        CheckConstraint(
            "requested_notional > 0",
            name="ck_trade_preview_notional_positive",
        ),
        CheckConstraint(
            "leverage >= 1 AND leverage <= 10",
            name="ck_trade_preview_leverage_range",
        ),
        CheckConstraint(
            "maximum_slippage > 0 AND maximum_slippage <= 0.25",
            name="ck_trade_preview_slippage_range",
        ),
        CheckConstraint(
            "emergency = false OR action = 'close'",
            name="ck_trade_preview_emergency_close",
        ),
    )


class TradeIntentRow(Base):
    __tablename__ = "trade_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paired_position_id: Mapped[str | None] = mapped_column(
        ForeignKey("paired_positions.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    environment: Mapped[str] = mapped_column(String(20))
    base_asset: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(20))
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), index=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    requested_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    base_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    spot_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        default=Decimal("0"),
    )
    perp_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        default=Decimal("0"),
    )
    market_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_version: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "leverage >= 1 AND leverage <= 10",
            name="ck_trade_intent_leverage_range",
        ),
        CheckConstraint(
            "emergency = false OR action = 'close'",
            name="ck_trade_intent_emergency_close",
        ),
    )


class OrderLegRow(Base):
    __tablename__ = "order_legs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trade_intent_id: Mapped[str] = mapped_column(
        ForeignKey("trade_intents.id", ondelete="CASCADE"),
        index=True,
    )
    leg: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(20))
    client_order_id: Mapped[str] = mapped_column(String(100), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    base_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        default=Decimal("1"),
    )
    limit_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        default=Decimal("0"),
    )
    average_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("trade_intent_id", "leg", name="uq_order_leg_intent_leg"),
        CheckConstraint(
            "base_multiplier > 0",
            name="ck_order_leg_base_multiplier_positive",
        ),
    )


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_leg_id: Mapped[str] = mapped_column(
        ForeignKey("order_legs.id", ondelete="CASCADE"),
        index=True,
    )
    exchange_trade_id: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fee_asset: Mapped[str] = mapped_column(String(40))
    liquidity: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "order_leg_id",
            "exchange_trade_id",
            name="uq_fill_leg_exchange_trade",
        ),
    )


class PairedPositionRow(Base):
    __tablename__ = "paired_positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    opening_intent_id: Mapped[str] = mapped_column(
        ForeignKey("trade_intents.id", ondelete="RESTRICT"),
        unique=True,
    )
    closing_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("trade_intents.id", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
    )
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    environment: Mapped[str] = mapped_column(String(20))
    base_asset: Mapped[str] = mapped_column(String(40))
    initial_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    spot_entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    opening_fees_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    remaining_opening_fees_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    closing_fees_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    realized_pnl_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class PnlRealizationRow(Base):
    __tablename__ = "pnl_realizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paired_position_id: Mapped[str] = mapped_column(
        ForeignKey("paired_positions.id", ondelete="RESTRICT"),
        index=True,
    )
    closing_intent_id: Mapped[str] = mapped_column(
        ForeignKey("trade_intents.id", ondelete="RESTRICT"),
        unique=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    gross_pnl_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    opening_fee_allocated_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18)
    )
    closing_fees_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    net_pnl_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    realized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    __table_args__ = (
        CheckConstraint(
            "quantity >= 0",
            name="ck_pnl_realization_quantity_nonnegative",
        ),
        CheckConstraint(
            "opening_fee_allocated_usdt >= 0",
            name="ck_pnl_realization_opening_fee_nonnegative",
        ),
        CheckConstraint(
            "closing_fees_usdt >= 0",
            name="ck_pnl_realization_closing_fees_nonnegative",
        ),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _numeric_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.000000000000001")


def _live_fees_usdt(fills: list[FillRow], base_asset: str) -> Decimal:
    total = Decimal("0")
    for item in fills:
        if item.fee_amount == 0:
            continue
        asset = item.fee_asset.upper()
        if asset in {"USDT", "USD"}:
            total += item.fee_amount
        elif asset == base_asset.upper():
            total += item.fee_amount * item.price
        else:
            raise ValueError("live fill fee asset cannot be valued in USDT")
    return total


def _compensation_client_order_id(intent: TradeIntentRow) -> str:
    token = intent.id.replace("-", "")[:20]
    if intent.exchange == "okx":
        return f"bhc{token}"
    if intent.exchange == "gate":
        return f"t-bhc-{token}"
    return f"bh-c-{token}"


def _notification_item(row: NotificationOutboxRow) -> NotificationOutboxItem:
    return NotificationOutboxItem(
        id=row.id,
        dedupe_key=row.dedupe_key,
        event_type=row.event_type,
        severity=row.severity,
        channel=row.channel,
        subject=row.subject,
        body=row.body,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=_utc(row.next_attempt_at),
        last_error_code=row.last_error_code,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        sent_at=_utc(row.sent_at) if row.sent_at is not None else None,
    )


def _live_compensation_leg(
    *,
    intent: TradeIntentRow,
    excess_leg: OrderLegRow,
    excess_base: Decimal,
    now: datetime,
) -> OrderLegRow:
    if excess_base <= 0:
        raise ValueError("live compensation quantity must be positive")
    if excess_leg.base_multiplier <= 0:
        raise ValueError("live compensation multiplier must be positive")
    side = "buy" if excess_leg.side == "sell" else "sell"
    reduce_only = (
        excess_leg.market == "perp"
        and side == "buy"
        and intent.action == "open"
    )
    return OrderLegRow(
        id=str(uuid.uuid4()),
        trade_intent_id=intent.id,
        leg=f"{excess_leg.leg}_compensation",
        market=excess_leg.market,
        symbol=excess_leg.symbol,
        side=side,
        client_order_id=_compensation_client_order_id(intent),
        status="created",
        quantity=excess_base / excess_leg.base_multiplier,
        base_multiplier=excess_leg.base_multiplier,
        limit_price=excess_leg.limit_price,
        filled_quantity=Decimal("0"),
        reduce_only=reduce_only,
        created_at=now,
        updated_at=now,
    )


def _compensation_pnl(
    *,
    primary: OrderLegRow,
    compensation: OrderLegRow,
    base_quantity: Decimal,
) -> Decimal:
    if primary.average_price is None or compensation.average_price is None:
        raise ValueError("live compensation prices are incomplete")
    if primary.side == "buy":
        return (
            compensation.average_price - primary.average_price
        ) * base_quantity
    return (
        primary.average_price - compensation.average_price
    ) * base_quantity


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            if self.engine.url.get_backend_name() == "sqlite":
                await connection.execute(text("PRAGMA journal_mode=WAL"))
                await connection.execute(text("PRAGMA busy_timeout=5000"))
                await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def executor_lock(self) -> AsyncIterator[bool]:
        connection: AsyncConnection | None = None
        if self.engine.url.get_backend_name() != "postgresql":
            yield True
            return
        connection = await self.engine.connect()
        lock_key = 7_284_217_119_035_423_281
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
            await connection.close()

    async def record_account_reconciliation(
        self,
        *,
        exchange: str,
        environment: str,
        status: str,
        reason: str,
        snapshot: Any | None = None,
        trading_state: Any | None = None,
        order_reconciliation_complete: bool = False,
        recovered_order_count: int = 0,
        fill_reconciliation_complete: bool = False,
        fill_count: int = 0,
        private_stream_ready: bool = False,
    ) -> None:
        async with self.sessions() as session:
            snapshot_id: str | None = None
            checked_at = datetime.now(UTC)
            if snapshot is not None:
                snapshot_id = str(uuid.uuid4())
                checked_at = snapshot.observed_at
                session.add(
                    AccountSnapshotRow(
                        id=snapshot_id,
                        exchange=exchange,
                        environment=environment,
                        observed_at=snapshot.observed_at,
                        spot_usdt_available=snapshot.spot_usdt_available,
                        perp_usdt_available=snapshot.perp_usdt_available,
                        perp_usdt_equity=snapshot.perp_usdt_equity,
                        shared_balance=snapshot.shared_balance,
                        account_mode=snapshot.account_mode,
                        position_mode=snapshot.position_mode.value,
                        trade_permission=snapshot.trade_permission,
                    )
                )
                if trading_state is not None:
                    session.add_all(
                        RemoteOpenOrderSnapshotRow(
                            id=str(uuid.uuid4()),
                            account_snapshot_id=snapshot_id,
                            exchange_order_id=item.exchange_order_id,
                            client_order_id=item.client_order_id,
                            market=item.market,
                            symbol=item.symbol,
                            side=item.side,
                            status=item.status,
                            price=item.price,
                            original_quantity=item.original_quantity,
                            filled_quantity=item.filled_quantity,
                            reduce_only=item.reduce_only,
                        )
                        for item in trading_state.open_orders
                    )
                    session.add_all(
                        RemotePositionSnapshotRow(
                            id=str(uuid.uuid4()),
                            account_snapshot_id=snapshot_id,
                            symbol=item.symbol,
                            side=item.side,
                            quantity=item.quantity,
                            entry_price=item.entry_price,
                            mark_price=item.mark_price,
                            liquidation_price=item.liquidation_price,
                            leverage=item.leverage,
                            isolated=item.isolated,
                        )
                        for item in trading_state.positions
                    )
            state_complete = bool(trading_state and trading_state.complete)
            open_order_count = len(trading_state.open_orders) if trading_state is not None else 0
            position_count = len(trading_state.positions) if trading_state is not None else 0
            row = await session.get(
                AccountReconciliationRow,
                {"exchange": exchange, "environment": environment},
            )
            if row is None:
                row = AccountReconciliationRow(
                    exchange=exchange,
                    environment=environment,
                    status=status,
                    reason=reason,
                    snapshot_id=snapshot_id,
                    trading_state_complete=state_complete,
                    order_reconciliation_complete=order_reconciliation_complete,
                    fill_reconciliation_complete=fill_reconciliation_complete,
                    private_stream_ready=private_stream_ready,
                    open_order_count=open_order_count,
                    position_count=position_count,
                    fill_count=fill_count,
                    recovered_order_count=recovered_order_count,
                    checked_at=checked_at,
                )
                session.add(row)
            else:
                row.status = status
                row.reason = reason
                row.snapshot_id = snapshot_id
                row.trading_state_complete = state_complete
                row.order_reconciliation_complete = order_reconciliation_complete
                row.fill_reconciliation_complete = fill_reconciliation_complete
                row.private_stream_ready = private_stream_ready
                row.open_order_count = open_order_count
                row.position_count = position_count
                row.fill_count = fill_count
                row.recovered_order_count = recovered_order_count
                row.checked_at = checked_at
            await session.commit()

    async def set_execution_control(self, *, state: str, reason: str) -> None:
        async with self.sessions() as session:
            row = await session.get(ExecutionControlRow, 1)
            now = datetime.now(UTC)
            if row is None:
                session.add(
                    ExecutionControlRow(
                        id=1,
                        state=state,
                        reason=reason,
                        updated_at=now,
                    )
                )
            else:
                row.state = state
                row.reason = reason
                row.updated_at = now
            await session.commit()

    async def execution_control(self) -> ExecutionControlRow | None:
        async with self.sessions() as session:
            return await session.get(ExecutionControlRow, 1)

    async def create_strategy_version(
        self,
        *,
        environment: str,
        payload: dict[str, Any],
        actor: str,
    ) -> StrategyVersionRow:
        async with self.sessions() as session:
            control = await session.scalar(
                select(AutomationControlRow)
                .where(AutomationControlRow.id == 1)
                .with_for_update()
            )
            if control is None:
                control = AutomationControlRow(
                    id=1,
                    state="disabled",
                    active_strategy_id=None,
                    reason="automatic trading is disabled by default",
                    updated_by="system",
                    updated_at=datetime.now(UTC),
                )
                session.add(control)
                await session.flush()
            latest = await session.scalar(
                select(func.max(StrategyVersionRow.version))
            )
            row = StrategyVersionRow(
                id=str(uuid.uuid4()),
                version=int(latest or 0) + 1,
                environment=environment,
                payload=json.dumps(
                    payload,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                created_by=actor,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def strategy_version(
        self,
        strategy_id: str,
    ) -> StrategyVersionRow | None:
        async with self.sessions() as session:
            return await session.get(StrategyVersionRow, strategy_id)

    async def latest_strategy_version(self) -> StrategyVersionRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(StrategyVersionRow)
                .order_by(StrategyVersionRow.version.desc())
                .limit(1)
            )

    async def automation_control(self) -> AutomationControlRow:
        async with self.sessions() as session:
            row = await session.get(AutomationControlRow, 1)
            if row is None:
                row = AutomationControlRow(
                    id=1,
                    state="disabled",
                    active_strategy_id=None,
                    reason="automatic trading is disabled by default",
                    updated_by="system",
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await session.get(AutomationControlRow, 1)
                    if existing is None:
                        raise
                    return existing
                await session.refresh(row)
            return row

    async def set_automation_control(
        self,
        *,
        state: str,
        active_strategy_id: str | None,
        reason: str,
        actor: str,
    ) -> AutomationControlRow:
        if state not in {"disabled", "enabled", "paused"}:
            raise ValueError("invalid automation state")
        async with self.sessions() as session:
            if active_strategy_id is not None:
                strategy = await session.get(
                    StrategyVersionRow,
                    active_strategy_id,
                )
                if strategy is None:
                    raise ValueError("strategy version was not found")
            elif state == "enabled":
                raise ValueError("enabled automation requires a strategy version")
            row = await session.scalar(
                select(AutomationControlRow)
                .where(AutomationControlRow.id == 1)
                .with_for_update()
            )
            now = datetime.now(UTC)
            if row is None:
                row = AutomationControlRow(
                    id=1,
                    state=state,
                    active_strategy_id=active_strategy_id,
                    reason=reason,
                    updated_by=actor,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.state = state
                row.active_strategy_id = active_strategy_id
                row.reason = reason
                row.updated_by = actor
                row.updated_at = now
            await session.commit()
            await session.refresh(row)
            return row

    async def set_private_stream_state(
        self,
        *,
        exchange: str,
        environment: str,
        connected: bool,
        authenticated: bool,
        orders_subscribed: bool,
        fills_subscribed: bool,
        positions_subscribed: bool,
        heartbeat: bool = False,
        event: bool = False,
    ) -> None:
        async with self.sessions() as session:
            now = datetime.now(UTC)
            row = await session.get(
                PrivateStreamStateRow,
                {"exchange": exchange, "environment": environment},
            )
            if row is None:
                row = PrivateStreamStateRow(
                    exchange=exchange,
                    environment=environment,
                    connected=connected,
                    authenticated=authenticated,
                    orders_subscribed=orders_subscribed,
                    fills_subscribed=fills_subscribed,
                    positions_subscribed=positions_subscribed,
                    last_heartbeat_at=now if heartbeat else None,
                    last_event_at=now if event else None,
                    disconnected_at=None if connected else now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.connected = connected
                row.authenticated = authenticated
                row.orders_subscribed = orders_subscribed
                row.fills_subscribed = fills_subscribed
                row.positions_subscribed = positions_subscribed
                if heartbeat:
                    row.last_heartbeat_at = now
                if event:
                    row.last_event_at = now
                row.disconnected_at = None if connected else now
                row.updated_at = now
            if not connected:
                control = await session.get(ExecutionControlRow, 1)
                if control is not None and control.state == "ready":
                    control.state = "paused"
                    control.reason = (
                        "private account event stream disconnected; "
                        "REST reconciliation is required"
                    )
                    control.updated_at = now
            await session.commit()

    async def private_stream_state(
        self,
        *,
        exchange: str,
        environment: str,
    ) -> PrivateStreamStateRow | None:
        async with self.sessions() as session:
            return await session.get(
                PrivateStreamStateRow,
                {"exchange": exchange, "environment": environment},
            )

    async def private_stream_ready(
        self,
        *,
        exchange: str,
        environment: str,
        maximum_heartbeat_age_seconds: int = 30,
    ) -> bool:
        row = await self.private_stream_state(
            exchange=exchange,
            environment=environment,
        )
        if row is None or row.last_heartbeat_at is None:
            return False
        heartbeat = _utc(row.last_heartbeat_at)
        return (
            row.connected
            and row.authenticated
            and row.orders_subscribed
            and row.fills_subscribed
            and row.positions_subscribed
            and heartbeat
            >= datetime.now(UTC)
            - timedelta(seconds=maximum_heartbeat_age_seconds)
        )

    async def reset_private_stream_states(self) -> None:
        async with self.sessions() as session:
            now = datetime.now(UTC)
            await session.execute(
                update(PrivateStreamStateRow).values(
                    connected=False,
                    authenticated=False,
                    orders_subscribed=False,
                    fills_subscribed=False,
                    positions_subscribed=False,
                    disconnected_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def reconciliation_states(self) -> list[AccountReconciliationRow]:
        async with self.sessions() as session:
            values = await session.scalars(
                select(AccountReconciliationRow).order_by(
                    AccountReconciliationRow.exchange,
                    AccountReconciliationRow.environment,
                )
            )
            return list(values)

    async def create_trade_preview(
        self,
        *,
        preview: dict[str, Any],
    ) -> TradePreviewRow:
        async with self.sessions() as session:
            row = TradePreviewRow(**preview)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def trade_preview(self, preview_id: str) -> TradePreviewRow | None:
        async with self.sessions() as session:
            return await session.get(TradePreviewRow, preview_id)

    async def reserve_trade_preview(
        self,
        *,
        preview_id: str,
        actor: str,
        request_fingerprint: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TradePreviewRow:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TradePreviewRow)
                .where(TradePreviewRow.id == preview_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("trade preview was not found")
            if row.actor != actor:
                raise ValueError("trade preview belongs to another administrator")
            if row.confirmation_idempotency_key is not None:
                if row.confirmation_idempotency_key != idempotency_key:
                    raise ValueError(
                        "trade preview was already confirmed with another "
                        "idempotency key"
                    )
                return row
            observed_now = now or datetime.now(UTC)
            if _utc(row.expires_at) <= observed_now:
                raise ValueError("trade preview has expired")
            if row.request_fingerprint != request_fingerprint:
                raise ValueError(
                    "market or configuration changed after trade preview"
                )
            row.confirmation_idempotency_key = idempotency_key
            row.confirmed_at = observed_now
            await session.commit()
            await session.refresh(row)
            return row

    async def create_trade_intent(
        self,
        *,
        intent: dict[str, Any],
        legs: list[dict[str, Any]],
    ) -> tuple[TradeIntentRow, list[OrderLegRow], bool]:
        async with self.sessions() as session:
            existing = await session.scalar(
                select(TradeIntentRow).where(
                    TradeIntentRow.idempotency_key == intent["idempotency_key"]
                )
            )
            if existing is not None:
                existing_legs = list(
                    await session.scalars(
                        select(OrderLegRow)
                        .where(OrderLegRow.trade_intent_id == existing.id)
                        .order_by(OrderLegRow.leg)
                    )
                )
                return existing, existing_legs, False
            row = TradeIntentRow(**intent)
            leg_rows = [OrderLegRow(**value) for value in legs]
            session.add(row)
            session.add_all(leg_rows)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(TradeIntentRow).where(
                        TradeIntentRow.idempotency_key == intent["idempotency_key"]
                    )
                )
                if existing is None:
                    raise
                existing_legs = list(
                    await session.scalars(
                        select(OrderLegRow)
                        .where(OrderLegRow.trade_intent_id == existing.id)
                        .order_by(OrderLegRow.leg)
                    )
                )
                return existing, existing_legs, False
            await session.refresh(row)
            return row, leg_rows, True

    async def create_paper_close_intent(
        self,
        *,
        position_id: str,
        intent: dict[str, Any],
        legs: list[dict[str, Any]],
    ) -> tuple[TradeIntentRow, list[OrderLegRow], bool]:
        async with self.sessions() as session:
            existing = await session.scalar(
                select(TradeIntentRow).where(
                    TradeIntentRow.idempotency_key == intent["idempotency_key"]
                )
            )
            if existing is not None:
                existing_legs = list(
                    await session.scalars(
                        select(OrderLegRow)
                        .where(OrderLegRow.trade_intent_id == existing.id)
                        .order_by(OrderLegRow.leg)
                    )
                )
                return existing, existing_legs, False
            position = await session.scalar(
                select(PairedPositionRow)
                .where(PairedPositionRow.id == position_id)
                .with_for_update()
            )
            if position is None:
                raise ValueError("paired position was not found")
            if position.status != "open" or position.closing_intent_id is not None:
                raise ValueError("paired position is not open")
            row = TradeIntentRow(**intent)
            leg_rows = [OrderLegRow(**value) for value in legs]
            position.status = "closing"
            position.closing_intent_id = row.id
            session.add(row)
            session.add_all(leg_rows)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(TradeIntentRow).where(
                        TradeIntentRow.idempotency_key == intent["idempotency_key"]
                    )
                )
                if existing is None:
                    raise
                existing_legs = list(
                    await session.scalars(
                        select(OrderLegRow)
                        .where(OrderLegRow.trade_intent_id == existing.id)
                        .order_by(OrderLegRow.leg)
                    )
                )
                return existing, existing_legs, False
            await session.refresh(row)
            return row, leg_rows, True

    async def paired_position(self, position_id: str) -> PairedPositionRow | None:
        async with self.sessions() as session:
            return await session.get(PairedPositionRow, position_id)

    async def trade_intent(self, intent_id: str) -> tuple[TradeIntentRow, list[OrderLegRow]] | None:
        async with self.sessions() as session:
            row = await session.get(TradeIntentRow, intent_id)
            if row is None:
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == row.id)
                    .order_by(OrderLegRow.leg)
                )
            )
            return row, legs

    async def trade_intent_by_idempotency(
        self, idempotency_key: str
    ) -> tuple[TradeIntentRow, list[OrderLegRow]] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TradeIntentRow).where(TradeIntentRow.idempotency_key == idempotency_key)
            )
            if row is None:
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == row.id)
                    .order_by(OrderLegRow.leg)
                )
            )
            return row, legs

    async def order_legs_for_reconciliation(
        self,
        *,
        exchange: str,
        environment: str,
    ) -> list[OrderLegRow]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(OrderLegRow)
                    .join(
                        TradeIntentRow,
                        OrderLegRow.trade_intent_id == TradeIntentRow.id,
                    )
                    .where(
                        TradeIntentRow.exchange == exchange,
                        TradeIntentRow.environment == environment,
                        TradeIntentRow.status.not_in({"closed", "failed"}),
                    )
                    .order_by(OrderLegRow.created_at, OrderLegRow.id)
                )
            )

    async def persist_remote_fills(
        self,
        *,
        order_leg_id: str,
        fills: list[RemoteFill],
    ) -> int:
        async with self.sessions() as session:
            leg = await session.scalar(
                select(OrderLegRow)
                .where(OrderLegRow.id == order_leg_id)
                .with_for_update()
            )
            if leg is None:
                raise ValueError("order leg was not found")
            for item in fills:
                if not item.exchange_trade_id:
                    raise ValueError("remote fill is missing an exchange trade ID")
                if item.quantity <= 0 or item.price <= 0:
                    raise ValueError("remote fill quantity and price must be positive")
                if item.occurred_at < _utc(leg.created_at) - timedelta(minutes=5):
                    raise ValueError("remote fill predates the local order leg")
                if item.market != leg.market or item.symbol != leg.symbol:
                    raise ValueError("remote fill does not match the local order leg")
                if item.side != leg.side:
                    raise ValueError("remote fill side does not match the local order leg")
                if item.client_order_id not in {None, leg.client_order_id}:
                    raise ValueError(
                        "remote fill client order ID does not match the local order leg"
                    )
                if (
                    leg.exchange_order_id is not None
                    and item.exchange_order_id != leg.exchange_order_id
                ):
                    raise ValueError(
                        "remote fill exchange order ID does not match the local order leg"
                    )
            exchange_order_ids = {
                item.exchange_order_id for item in fills if item.exchange_order_id
            }
            if leg.exchange_order_id is None and exchange_order_ids:
                if len(exchange_order_ids) != 1:
                    raise ValueError("remote fills contain multiple exchange order IDs")
                leg.exchange_order_id = exchange_order_ids.pop()
            existing_rows = list(
                await session.scalars(
                    select(FillRow).where(FillRow.order_leg_id == leg.id)
                )
            )
            existing_by_trade = {
                item.exchange_trade_id: item for item in existing_rows
            }
            for item in fills:
                existing = existing_by_trade.get(item.exchange_trade_id)
                if existing is not None and (
                    not _numeric_equal(existing.quantity, item.quantity)
                    or not _numeric_equal(existing.price, item.price)
                    or not _numeric_equal(existing.fee_amount, item.fee_amount)
                    or existing.fee_asset != item.fee_asset
                    or existing.liquidity != item.liquidity
                    or _utc(existing.occurred_at) != _utc(item.occurred_at)
                ):
                    raise ValueError("remote fill changed after it was persisted")
            new_rows: list[FillRow] = []
            seen_ids = set(existing_by_trade)
            for item in fills:
                if item.exchange_trade_id in seen_ids:
                    continue
                new_rows.append(
                    FillRow(
                        id=str(uuid.uuid4()),
                        order_leg_id=leg.id,
                        exchange_trade_id=item.exchange_trade_id,
                        quantity=item.quantity,
                        price=item.price,
                        fee_amount=item.fee_amount,
                        fee_asset=item.fee_asset,
                        liquidity=item.liquidity,
                        occurred_at=item.occurred_at,
                    )
                )
                seen_ids.add(item.exchange_trade_id)
            session.add_all(new_rows)
            await session.flush()
            filled_quantity = (
                await session.scalar(
                    select(func.sum(FillRow.quantity)).where(
                        FillRow.order_leg_id == leg.id
                    )
                )
                or Decimal("0")
            )
            if filled_quantity > leg.quantity:
                raise ValueError("remote fills exceed the local order quantity")
            filled_notional = (
                await session.scalar(
                    select(func.sum(FillRow.quantity * FillRow.price)).where(
                        FillRow.order_leg_id == leg.id
                    )
                )
                or Decimal("0")
            )
            leg.filled_quantity = filled_quantity
            leg.average_price = (
                filled_notional / filled_quantity if filled_quantity > 0 else None
            )
            if filled_quantity >= leg.quantity:
                leg.status = "filled"
            elif filled_quantity > 0 and leg.status not in {"canceled", "failed"}:
                leg.status = "partially_filled"
            leg.updated_at = datetime.now(UTC)
            await session.commit()
            return len(new_rows)

    async def reconcile_remote_order(
        self,
        *,
        order_leg_id: str,
        order: RemoteOrder,
    ) -> str:
        async with self.sessions() as session:
            leg = await session.scalar(
                select(OrderLegRow)
                .where(OrderLegRow.id == order_leg_id)
                .with_for_update()
            )
            if leg is None:
                raise ValueError("order leg was not found")
            if not order.exchange_order_id:
                raise ValueError("remote order is missing an exchange order ID")
            if order.client_order_id != leg.client_order_id:
                raise ValueError(
                    "remote order client ID does not match the local order leg"
                )
            if order.market != leg.market or order.symbol != leg.symbol:
                raise ValueError("remote order does not match the local order leg")
            if order.side != leg.side:
                raise ValueError("remote order side does not match the local order leg")
            if not _numeric_equal(order.original_quantity, leg.quantity):
                raise ValueError(
                    "remote order quantity does not match the local order leg"
                )
            if order.filled_quantity < 0 or order.filled_quantity > order.original_quantity:
                raise ValueError("remote order filled quantity is invalid")
            if order.reduce_only != leg.reduce_only:
                raise ValueError(
                    "remote order reduce-only flag does not match the local order leg"
                )
            if (
                leg.exchange_order_id is not None
                and leg.exchange_order_id != order.exchange_order_id
            ):
                raise ValueError(
                    "remote order ID does not match the linked local order leg"
                )
            leg.exchange_order_id = order.exchange_order_id
            remote_status = order.status.strip().lower()
            if remote_status in {
                "cancelled",
                "canceled",
                "deactivated",
                "expired",
                "expired_in_match",
                "partiallyfilledcanceled",
                "partially_filled_canceled",
                "finished",
                "4",
            }:
                leg.status = "canceled"
            elif remote_status in {
                "failed",
                "invalid",
                "rejected",
                "5",
            }:
                leg.status = "failed"
            elif leg.status in {"submitted", "unknown"}:
                # A query response proves exchange acceptance, not execution.
                # Filled state remains derived from persisted trade records.
                leg.status = "acknowledged"
            leg.updated_at = datetime.now(UTC)
            await session.commit()
            return order.exchange_order_id

    async def list_trade_intents(self, *, limit: int = 100) -> list[TradeIntentRow]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(TradeIntentRow).order_by(TradeIntentRow.created_at.desc()).limit(limit)
                )
            )

    async def recoverable_trade_intents(self) -> list[TradeIntentRow]:
        terminal = {"closed", "failed"}
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(TradeIntentRow)
                    .where(TradeIntentRow.status.not_in(terminal))
                    .order_by(TradeIntentRow.created_at)
                )
            )

    async def transition_trade_intent(
        self,
        *,
        intent_id: str,
        expected_version: int,
        status: str,
    ) -> TradeIntentRow | None:
        async with self.sessions() as session:
            result = await session.execute(
                update(TradeIntentRow)
                .where(
                    TradeIntentRow.id == intent_id,
                    TradeIntentRow.version == expected_version,
                )
                .values(
                    status=status,
                    version=TradeIntentRow.version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            if not result.rowcount:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(TradeIntentRow, intent_id)

    async def prepare_live_submission(
        self,
        *,
        intent_id: str,
    ) -> tuple[TradeIntentRow, list[OrderLegRow], bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow)
                .where(TradeIntentRow.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .order_by(OrderLegRow.leg)
                    .with_for_update()
                )
            )
            if intent.environment not in {"sandbox", "live"}:
                raise ValueError("only exchange-backed intents can be submitted")
            if intent.action not in {"open", "close"}:
                raise ValueError("unsupported live intent action")
            primary = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
            if set(primary) != {"spot", "perp"} or len(legs) != 2:
                raise ValueError("live intent must contain exactly two primary legs")
            if intent.status != "planned":
                return intent, legs, False
            control = await session.scalar(
                select(ExecutionControlRow)
                .where(ExecutionControlRow.id == 1)
                .with_for_update()
            )
            allowed_control_states = (
                {"ready", "paused"}
                if intent.emergency and intent.action == "close"
                else {"ready"}
            )
            if (
                control is None
                or control.state not in allowed_control_states
            ):
                return intent, legs, False
            if any(item.status != "created" for item in primary.values()):
                raise ValueError("live order legs are not ready for first submission")
            if intent.action == "close":
                if intent.paired_position_id is None:
                    raise ValueError("live close intent is missing its paired position")
                position = await session.scalar(
                    select(PairedPositionRow)
                    .where(PairedPositionRow.id == intent.paired_position_id)
                    .with_for_update()
                )
                if (
                    position is None
                    or position.status != "closing"
                    or position.closing_intent_id != intent.id
                ):
                    raise ValueError(
                        "paired position is not reserved by the live close intent"
                    )
                spot_base = (
                    primary["spot"].quantity
                    * primary["spot"].base_multiplier
                )
                perp_base = (
                    primary["perp"].quantity
                    * primary["perp"].base_multiplier
                )
                if (
                    not _numeric_equal(spot_base, position.quantity)
                    or not _numeric_equal(perp_base, position.quantity)
                    or primary["spot"].side != "sell"
                    or primary["spot"].reduce_only
                    or primary["perp"].side != "buy"
                    or not primary["perp"].reduce_only
                ):
                    raise ValueError(
                        "live close legs do not exactly reduce the paired position"
                    )
            now = datetime.now(UTC)
            intent.status = "executing"
            intent.version += 1
            intent.updated_at = now
            for item in primary.values():
                item.status = "submitted"
                item.updated_at = now
            await session.commit()
            return intent, legs, True

    async def prepare_live_compensation(
        self,
        *,
        intent_id: str,
        limit_price: Decimal,
    ) -> tuple[TradeIntentRow, OrderLegRow, bool] | None:
        if limit_price <= 0:
            raise ValueError("live compensation limit price must be positive")
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow)
                .where(TradeIntentRow.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .with_for_update()
                )
            )
            compensations = [
                item for item in legs if item.leg.endswith("_compensation")
            ]
            if len(compensations) != 1:
                raise ValueError(
                    "compensating intent must contain one protection leg"
                )
            compensation = compensations[0]
            if intent.status != "compensating":
                return intent, compensation, False
            if compensation.status != "created":
                return intent, compensation, False
            control = await session.get(ExecutionControlRow, 1)
            if control is None or control.state != "paused":
                return intent, compensation, False
            compensation.limit_price = limit_price
            compensation.status = "submitted"
            now = datetime.now(UTC)
            compensation.updated_at = now
            session.add(
                AuditEventRow(
                    id=str(uuid.uuid4()),
                    occurred_at=now,
                    event_type="trade.compensation_submitted",
                    actor="system",
                    details=json.dumps(
                        {
                            "intent_id": intent.id,
                            "order_leg_id": compensation.id,
                            "market": compensation.market,
                            "side": compensation.side,
                            "quantity": format(
                                compensation.quantity,
                                "f",
                            ),
                            "limit_price": format(limit_price, "f"),
                            "reduce_only": compensation.reduce_only,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
            return intent, compensation, True

    async def expire_planned_trade_intent(
        self,
        *,
        intent_id: str,
    ) -> bool:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow)
                .where(TradeIntentRow.id == intent_id)
                .with_for_update()
            )
            if (
                intent is None
                or intent.environment not in {"sandbox", "live"}
                or intent.status != "planned"
            ):
                return False
            now = datetime.now(UTC)
            if intent.action == "close" and intent.paired_position_id is not None:
                position = await session.scalar(
                    select(PairedPositionRow)
                    .where(
                        PairedPositionRow.id == intent.paired_position_id
                    )
                    .with_for_update()
                )
                if (
                    position is not None
                    and position.status == "closing"
                    and position.closing_intent_id == intent.id
                ):
                    position.status = "open"
                    position.closing_intent_id = None
            intent.status = "failed"
            intent.version += 1
            intent.updated_at = now
            await session.commit()
            return True

    async def record_order_submission(
        self,
        *,
        order_leg_id: str,
        submission: OrderSubmission,
    ) -> None:
        async with self.sessions() as session:
            leg = await session.scalar(
                select(OrderLegRow)
                .where(OrderLegRow.id == order_leg_id)
                .with_for_update()
            )
            if leg is None:
                raise ValueError("order leg was not found")
            if leg.status not in {"submitted", "acknowledged"}:
                raise ValueError("order leg is not awaiting an acknowledgement")
            if (
                submission.market != leg.market
                or submission.symbol != leg.symbol
                or submission.client_order_id != leg.client_order_id
            ):
                raise ValueError("order acknowledgement does not match the local leg")
            if (
                leg.exchange_order_id is not None
                and submission.exchange_order_id is not None
                and leg.exchange_order_id != submission.exchange_order_id
            ):
                raise ValueError("order acknowledgement changed exchange order ID")
            if submission.exchange_order_id is not None:
                leg.exchange_order_id = submission.exchange_order_id
            leg.status = "acknowledged"
            leg.updated_at = datetime.now(UTC)
            await session.commit()

    async def mark_order_submission_unknown(
        self,
        *,
        order_leg_id: str,
    ) -> None:
        async with self.sessions() as session:
            leg = await session.scalar(
                select(OrderLegRow)
                .where(OrderLegRow.id == order_leg_id)
                .with_for_update()
            )
            if leg is None:
                raise ValueError("order leg was not found")
            if leg.status == "submitted":
                leg.status = "unknown"
                leg.updated_at = datetime.now(UTC)
            control = await session.get(ExecutionControlRow, 1)
            reason = (
                "live order acknowledgement is uncertain; "
                "client-order-ID reconciliation is required"
            )
            now = datetime.now(UTC)
            if control is None:
                session.add(
                    ExecutionControlRow(
                        id=1,
                        state="paused",
                        reason=reason,
                        updated_at=now,
                    )
                )
            else:
                control.state = "paused"
                control.reason = reason
                control.updated_at = now
            await session.commit()

    async def settle_live_open(
        self,
        *,
        intent_id: str,
    ) -> tuple[TradeIntentRow, PairedPositionRow | None, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow)
                .where(TradeIntentRow.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                return None
            position = await session.scalar(
                select(PairedPositionRow).where(
                    PairedPositionRow.opening_intent_id == intent.id
                )
            )
            if position is not None:
                return intent, position, False
            if (
                intent.environment not in {"sandbox", "live"}
                or intent.action != "open"
                or intent.status not in {"executing", "compensating"}
            ):
                return intent, None, False
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .order_by(OrderLegRow.leg)
                    .with_for_update()
                )
            )
            primary = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
            compensations = [
                item for item in legs if item.leg.endswith("_compensation")
            ]
            if (
                set(primary) != {"spot", "perp"}
                or len(compensations) > 1
                or len(legs) != 2 + len(compensations)
            ):
                raise ValueError(
                    "live intent contains an invalid compensation layout"
                )
            terminal = {"filled", "canceled", "failed"}
            if any(item.status not in terminal for item in primary.values()):
                return intent, None, False
            spot_base = (
                primary["spot"].filled_quantity
                * primary["spot"].base_multiplier
            )
            perp_base = (
                primary["perp"].filled_quantity
                * primary["perp"].base_multiplier
            )
            common_base = min(spot_base, perp_base)
            now = datetime.now(UTC)
            changed = True
            if spot_base == 0 and perp_base == 0:
                intent.status = "failed"
            elif not _numeric_equal(spot_base, perp_base):
                excess_leg = (
                    primary["spot"]
                    if spot_base > perp_base
                    else primary["perp"]
                )
                excess_base = abs(spot_base - perp_base)
                if intent.status == "executing" and not compensations:
                    compensation = _live_compensation_leg(
                        intent=intent,
                        excess_leg=excess_leg,
                        excess_base=excess_base,
                        now=now,
                    )
                    session.add(compensation)
                    session.add(
                        AuditEventRow(
                            id=str(uuid.uuid4()),
                            occurred_at=now,
                            event_type="trade.compensation_required",
                            actor="system",
                            details=json.dumps(
                                {
                                    "intent_id": intent.id,
                                    "order_leg_id": compensation.id,
                                    "action": intent.action,
                                    "market": compensation.market,
                                    "side": compensation.side,
                                    "base_quantity": format(
                                        excess_base,
                                        "f",
                                    ),
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
                    intent.status = "compensating"
                    await self._pause_live_settlement(session, now)
                    intent.version += 1
                    intent.updated_at = now
                    await session.commit()
                    return intent, None, True
                compensation = (
                    compensations[0] if len(compensations) == 1 else None
                )
                if (
                    compensation is not None
                    and compensation.status
                    not in {"filled", "canceled", "failed"}
                ):
                    return intent, None, False
                compensated_base = (
                    compensation.filled_quantity
                    * compensation.base_multiplier
                    if compensation is not None
                    else Decimal("0")
                )
                if (
                    compensation is None
                    or not _numeric_equal(compensated_base, excess_base)
                    or compensation.average_price is None
                ):
                    intent.status = "manual_review"
                    await self._pause_live_settlement(session, now)
                else:
                    fill_rows = list(
                        await session.scalars(
                            select(FillRow)
                            .join(
                                OrderLegRow,
                                FillRow.order_leg_id == OrderLegRow.id,
                            )
                            .where(OrderLegRow.trade_intent_id == intent.id)
                        )
                    )
                    try:
                        fees = _live_fees_usdt(
                            fill_rows,
                            intent.base_asset,
                        )
                        compensation_profit = _compensation_pnl(
                            primary=excess_leg,
                            compensation=compensation,
                            base_quantity=excess_base,
                        )
                    except ValueError:
                        intent.status = "manual_review"
                        await self._pause_live_settlement(session, now)
                    else:
                        if common_base == 0:
                            intent.status = "failed"
                        elif (
                            primary["spot"].average_price is None
                            or primary["perp"].average_price is None
                        ):
                            intent.status = "manual_review"
                            await self._pause_live_settlement(session, now)
                        else:
                            opening_cost = fees - compensation_profit
                            position = PairedPositionRow(
                                id=str(uuid.uuid4()),
                                opening_intent_id=intent.id,
                                exchange=intent.exchange,
                                environment=intent.environment,
                                base_asset=intent.base_asset,
                                initial_quantity=common_base,
                                quantity=common_base,
                                spot_entry_price=primary[
                                    "spot"
                                ].average_price,
                                perp_entry_price=primary[
                                    "perp"
                                ].average_price,
                                opening_fees_usdt=opening_cost,
                                remaining_opening_fees_usdt=opening_cost,
                                status="open",
                                opened_at=now,
                            )
                            session.add(position)
                            intent.status = "hedged"
            elif (
                primary["spot"].average_price is None
                or primary["perp"].average_price is None
            ):
                intent.status = "manual_review"
                await self._pause_live_settlement(session, now)
            else:
                fill_rows = list(
                    await session.scalars(
                        select(FillRow)
                        .join(
                            OrderLegRow,
                            FillRow.order_leg_id == OrderLegRow.id,
                        )
                        .where(OrderLegRow.trade_intent_id == intent.id)
                    )
                )
                try:
                    fees = _live_fees_usdt(fill_rows, intent.base_asset)
                except ValueError:
                    intent.status = "manual_review"
                    await self._pause_live_settlement(session, now)
                else:
                    position = PairedPositionRow(
                        id=str(uuid.uuid4()),
                        opening_intent_id=intent.id,
                        exchange=intent.exchange,
                        environment=intent.environment,
                        base_asset=intent.base_asset,
                        initial_quantity=common_base,
                        quantity=common_base,
                        spot_entry_price=primary["spot"].average_price,
                        perp_entry_price=primary["perp"].average_price,
                        opening_fees_usdt=fees,
                        remaining_opening_fees_usdt=fees,
                        status="open",
                        opened_at=now,
                    )
                    session.add(position)
                    intent.status = "hedged"
            intent.version += 1
            intent.updated_at = now
            await session.commit()
            return intent, position, changed

    async def settle_live_close(
        self,
        *,
        intent_id: str,
    ) -> tuple[TradeIntentRow, PairedPositionRow | None, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow)
                .where(TradeIntentRow.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                return None
            if intent.paired_position_id is None:
                return intent, None, False
            position = await session.scalar(
                select(PairedPositionRow)
                .where(PairedPositionRow.id == intent.paired_position_id)
                .with_for_update()
            )
            if position is None:
                return intent, None, False
            if intent.status in {"closed", "failed"}:
                return intent, position, False
            if (
                intent.environment not in {"sandbox", "live"}
                or intent.action != "close"
                or intent.status not in {"executing", "compensating"}
                or position.status != "closing"
                or position.closing_intent_id != intent.id
            ):
                return intent, position, False
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .order_by(OrderLegRow.leg)
                    .with_for_update()
                )
            )
            primary = {
                item.leg: item
                for item in legs
                if item.leg in {"spot", "perp"}
            }
            compensations = [
                item for item in legs if item.leg.endswith("_compensation")
            ]
            if (
                set(primary) != {"spot", "perp"}
                or len(compensations) > 1
                or len(legs) != 2 + len(compensations)
            ):
                raise ValueError(
                    "live intent contains an invalid compensation layout"
                )
            terminal = {"filled", "canceled", "failed"}
            if any(item.status not in terminal for item in primary.values()):
                return intent, position, False
            spot_base = (
                primary["spot"].filled_quantity
                * primary["spot"].base_multiplier
            )
            perp_base = (
                primary["perp"].filled_quantity
                * primary["perp"].base_multiplier
            )
            now = datetime.now(UTC)
            if spot_base == 0 and perp_base == 0:
                intent.status = "failed"
                position.status = "open"
                position.closing_intent_id = None
            elif not _numeric_equal(spot_base, perp_base):
                common_base = min(spot_base, perp_base)
                excess_leg = (
                    primary["spot"]
                    if spot_base > perp_base
                    else primary["perp"]
                )
                excess_base = abs(spot_base - perp_base)
                if intent.status == "executing" and not compensations:
                    compensation = _live_compensation_leg(
                        intent=intent,
                        excess_leg=excess_leg,
                        excess_base=excess_base,
                        now=now,
                    )
                    session.add(compensation)
                    session.add(
                        AuditEventRow(
                            id=str(uuid.uuid4()),
                            occurred_at=now,
                            event_type="trade.compensation_required",
                            actor="system",
                            details=json.dumps(
                                {
                                    "intent_id": intent.id,
                                    "order_leg_id": compensation.id,
                                    "action": intent.action,
                                    "market": compensation.market,
                                    "side": compensation.side,
                                    "base_quantity": format(
                                        excess_base,
                                        "f",
                                    ),
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
                    intent.status = "compensating"
                    await self._pause_live_settlement(session, now)
                    intent.version += 1
                    intent.updated_at = now
                    await session.commit()
                    return intent, position, True
                compensation = (
                    compensations[0] if len(compensations) == 1 else None
                )
                if (
                    compensation is not None
                    and compensation.status
                    not in {"filled", "canceled", "failed"}
                ):
                    return intent, position, False
                compensated_base = (
                    compensation.filled_quantity
                    * compensation.base_multiplier
                    if compensation is not None
                    else Decimal("0")
                )
                if (
                    common_base > position.quantity
                    or compensation is None
                    or not _numeric_equal(compensated_base, excess_base)
                    or compensation.average_price is None
                ):
                    intent.status = "manual_review"
                    await self._pause_live_settlement(session, now)
                else:
                    fill_rows = list(
                        await session.scalars(
                            select(FillRow)
                            .join(
                                OrderLegRow,
                                FillRow.order_leg_id == OrderLegRow.id,
                            )
                            .where(OrderLegRow.trade_intent_id == intent.id)
                        )
                    )
                    try:
                        closing_fees = _live_fees_usdt(
                            fill_rows,
                            intent.base_asset,
                        )
                        compensation_profit = _compensation_pnl(
                            primary=excess_leg,
                            compensation=compensation,
                            base_quantity=excess_base,
                        )
                    except ValueError:
                        intent.status = "manual_review"
                        await self._pause_live_settlement(session, now)
                    else:
                        opening_fee_allocation = (
                            Decimal("0")
                            if common_base == 0
                            else (
                                position.remaining_opening_fees_usdt
                                if _numeric_equal(
                                    common_base,
                                    position.quantity,
                                )
                                else (
                                    position.remaining_opening_fees_usdt
                                    * common_base
                                    / position.quantity
                                )
                            )
                        )
                        common_gross_pnl = (
                            Decimal("0")
                            if common_base == 0
                            else (
                                (
                                    primary["spot"].average_price
                                    - position.spot_entry_price
                                )
                                + (
                                    position.perp_entry_price
                                    - primary["perp"].average_price
                                )
                            )
                            * common_base
                        )
                        gross_pnl = (
                            common_gross_pnl + compensation_profit
                        )
                        net_pnl = (
                            gross_pnl
                            - opening_fee_allocation
                            - closing_fees
                        )
                        position.closing_fees_usdt = (
                            position.closing_fees_usdt
                            or Decimal("0")
                        ) + closing_fees
                        position.realized_pnl_usdt = (
                            position.realized_pnl_usdt
                            or Decimal("0")
                        ) + net_pnl
                        position.remaining_opening_fees_usdt -= (
                            opening_fee_allocation
                        )
                        position.quantity -= common_base
                        session.add(
                            PnlRealizationRow(
                                id=str(uuid.uuid4()),
                                paired_position_id=position.id,
                                closing_intent_id=intent.id,
                                quantity=common_base,
                                gross_pnl_usdt=gross_pnl,
                                opening_fee_allocated_usdt=(
                                    opening_fee_allocation
                                ),
                                closing_fees_usdt=closing_fees,
                                net_pnl_usdt=net_pnl,
                                realized_at=now,
                            )
                        )
                        intent.status = (
                            "closed" if common_base > 0 else "failed"
                        )
                        if _numeric_equal(
                            position.quantity,
                            Decimal("0"),
                        ):
                            position.quantity = Decimal("0")
                            position.status = "closed"
                            position.closed_at = now
                        else:
                            position.status = "open"
                            position.closing_intent_id = None
            elif (
                spot_base > position.quantity
                or primary["spot"].average_price is None
                or primary["perp"].average_price is None
            ):
                intent.status = "manual_review"
                await self._pause_live_settlement(session, now)
            else:
                fill_rows = list(
                    await session.scalars(
                        select(FillRow)
                        .join(
                            OrderLegRow,
                            FillRow.order_leg_id == OrderLegRow.id,
                        )
                        .where(OrderLegRow.trade_intent_id == intent.id)
                    )
                )
                try:
                    closing_fees = _live_fees_usdt(
                        fill_rows,
                        intent.base_asset,
                    )
                except ValueError:
                    intent.status = "manual_review"
                    await self._pause_live_settlement(session, now)
                else:
                    opening_fee_allocation = (
                        position.remaining_opening_fees_usdt
                        if _numeric_equal(spot_base, position.quantity)
                        else (
                            position.remaining_opening_fees_usdt
                            * spot_base
                            / position.quantity
                        )
                    )
                    gross_pnl = (
                        (
                            primary["spot"].average_price
                            - position.spot_entry_price
                        )
                        + (
                            position.perp_entry_price
                            - primary["perp"].average_price
                        )
                    ) * spot_base
                    net_pnl = (
                        gross_pnl
                        - opening_fee_allocation
                        - closing_fees
                    )
                    position.closing_fees_usdt = (
                        position.closing_fees_usdt or Decimal("0")
                    ) + closing_fees
                    position.realized_pnl_usdt = (
                        position.realized_pnl_usdt or Decimal("0")
                    ) + net_pnl
                    position.remaining_opening_fees_usdt -= (
                        opening_fee_allocation
                    )
                    position.quantity -= spot_base
                    session.add(
                        PnlRealizationRow(
                            id=str(uuid.uuid4()),
                            paired_position_id=position.id,
                            closing_intent_id=intent.id,
                            quantity=spot_base,
                            gross_pnl_usdt=gross_pnl,
                            opening_fee_allocated_usdt=(
                                opening_fee_allocation
                            ),
                            closing_fees_usdt=closing_fees,
                            net_pnl_usdt=net_pnl,
                            realized_at=now,
                        )
                    )
                    intent.status = "closed"
                    if _numeric_equal(position.quantity, Decimal("0")):
                        position.quantity = Decimal("0")
                        position.status = "closed"
                        position.closed_at = now
                    else:
                        position.status = "open"
                        position.closing_intent_id = None
            intent.version += 1
            intent.updated_at = now
            await session.commit()
            return intent, position, True

    @staticmethod
    async def _pause_live_settlement(
        session: AsyncSession,
        now: datetime,
    ) -> None:
        control = await session.get(ExecutionControlRow, 1)
        reason = (
            "live paired fills are imbalanced or cannot be valued; "
            "manual exposure review is required"
        )
        if control is None:
            session.add(
                ExecutionControlRow(
                    id=1,
                    state="paused",
                    reason=reason,
                    updated_at=now,
                )
            )
        else:
            control.state = "paused"
            control.reason = reason
            control.updated_at = now

    async def execute_paper_open(
        self,
        *,
        intent_id: str,
    ) -> tuple[TradeIntentRow, PairedPositionRow | None, bool] | None:
        value = await self.trade_intent(intent_id)
        if value is None:
            return None
        _, legs = value
        primary = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
        if set(primary) != {"spot", "perp"}:
            return None
        return await self.record_paper_open_fills(
            intent_id=intent_id,
            spot_fill_quantity=primary["spot"].quantity,
            perp_fill_quantity=primary["perp"].quantity,
        )

    async def record_paper_open_fills(
        self,
        *,
        intent_id: str,
        spot_fill_quantity: Decimal,
        perp_fill_quantity: Decimal,
    ) -> tuple[TradeIntentRow, PairedPositionRow | None, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow).where(TradeIntentRow.id == intent_id).with_for_update()
            )
            if intent is None:
                return None
            existing_position = await session.scalar(
                select(PairedPositionRow).where(PairedPositionRow.opening_intent_id == intent.id)
            )
            if existing_position is not None:
                return intent, existing_position, False
            if (
                intent.environment != "paper"
                or intent.action != "open"
                or intent.status != "planned"
            ):
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .with_for_update()
                )
            )
            by_leg = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
            if set(by_leg) != {"spot", "perp"}:
                return None
            fill_quantities = {
                "spot": spot_fill_quantity,
                "perp": perp_fill_quantity,
            }
            if any(
                quantity < 0 or quantity > by_leg[leg_name].quantity
                for leg_name, quantity in fill_quantities.items()
            ):
                raise ValueError("paper fill quantity is outside the order quantity")
            now = datetime.now(UTC)
            fills: list[FillRow] = []
            total_fees = Decimal("0")
            for leg_name, leg in by_leg.items():
                filled_quantity = fill_quantities[leg_name]
                fee_rate = intent.spot_fee_rate if leg_name == "spot" else intent.perp_fee_rate
                fee = filled_quantity * leg.limit_price * fee_rate
                total_fees += fee
                leg.exchange_order_id = f"paper:{leg.id}"
                leg.status = (
                    "filled"
                    if filled_quantity == leg.quantity
                    else "partially_filled"
                    if filled_quantity > 0
                    else "canceled"
                )
                leg.filled_quantity = filled_quantity
                leg.average_price = leg.limit_price if filled_quantity > 0 else None
                leg.updated_at = now
                if filled_quantity > 0:
                    fills.append(
                        FillRow(
                            id=str(uuid.uuid4()),
                            order_leg_id=leg.id,
                            exchange_trade_id=f"paper:{leg.id}:fill",
                            quantity=filled_quantity,
                            price=leg.limit_price,
                            fee_amount=fee,
                            fee_asset="USDT",
                            liquidity="taker",
                            occurred_at=now,
                        )
                    )
            common_quantity = min(spot_fill_quantity, perp_fill_quantity)
            excess_quantity = abs(spot_fill_quantity - perp_fill_quantity)
            position: PairedPositionRow | None = None
            if excess_quantity:
                excess_leg = (
                    by_leg["spot"] if spot_fill_quantity > perp_fill_quantity else by_leg["perp"]
                )
                compensation_leg_name = f"{excess_leg.leg}_compensation"
                compensation_side = "sell" if excess_leg.side == "buy" else "buy"
                session.add(
                    OrderLegRow(
                        id=str(uuid.uuid4()),
                        trade_intent_id=intent.id,
                        leg=compensation_leg_name,
                        market=excess_leg.market,
                        symbol=excess_leg.symbol,
                        side=compensation_side,
                        client_order_id=(f"{excess_leg.client_order_id.rsplit('-', 1)[0]}-c"),
                        status="created",
                        quantity=excess_quantity,
                        limit_price=excess_leg.limit_price,
                        filled_quantity=Decimal("0"),
                        reduce_only=excess_leg.market == "perp",
                        created_at=now,
                        updated_at=now,
                    )
                )
                intent.status = "compensating"
            elif common_quantity > 0:
                position = PairedPositionRow(
                    id=str(uuid.uuid4()),
                    opening_intent_id=intent.id,
                    exchange=intent.exchange,
                    environment=intent.environment,
                    base_asset=intent.base_asset,
                    initial_quantity=common_quantity,
                    quantity=common_quantity,
                    spot_entry_price=by_leg["spot"].limit_price,
                    perp_entry_price=by_leg["perp"].limit_price,
                    opening_fees_usdt=total_fees,
                    remaining_opening_fees_usdt=total_fees,
                    status="open",
                    opened_at=now,
                )
                session.add(position)
                intent.status = "hedged"
            else:
                intent.status = "failed"
            intent.version += 1
            intent.updated_at = now
            session.add_all(fills)
            await session.commit()
            await session.refresh(intent)
            if position is not None:
                await session.refresh(position)
            return intent, position, True

    async def execute_paper_compensation(
        self,
        *,
        intent_id: str,
        succeeds: bool = True,
    ) -> tuple[TradeIntentRow, PairedPositionRow | None, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow).where(TradeIntentRow.id == intent_id).with_for_update()
            )
            if intent is None:
                return None
            existing_position = await session.scalar(
                select(PairedPositionRow).where(PairedPositionRow.opening_intent_id == intent.id)
            )
            if existing_position is not None:
                return intent, existing_position, False
            if (
                intent.environment != "paper"
                or intent.action != "open"
                or intent.status != "compensating"
            ):
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .with_for_update()
                )
            )
            by_leg = {item.leg: item for item in legs}
            primary = {name: by_leg.get(name) for name in ("spot", "perp")}
            compensations = [item for item in legs if item.leg.endswith("_compensation")]
            if any(item is None for item in primary.values()) or len(compensations) != 1:
                return None
            compensation = compensations[0]
            now = datetime.now(UTC)
            if not succeeds:
                compensation.status = "failed"
                compensation.updated_at = now
                intent.status = "manual_review"
                intent.version += 1
                intent.updated_at = now
                control = await session.get(ExecutionControlRow, 1)
                reason = "paired trade compensation failed; manual exposure review is required"
                if control is None:
                    session.add(
                        ExecutionControlRow(
                            id=1,
                            state="paused",
                            reason=reason,
                            updated_at=now,
                        )
                    )
                else:
                    control.state = "paused"
                    control.reason = reason
                    control.updated_at = now
                await session.commit()
                await session.refresh(intent)
                return intent, None, True

            compensation_fee_rate = (
                intent.spot_fee_rate if compensation.market == "spot" else intent.perp_fee_rate
            )
            compensation_fee = (
                compensation.quantity * compensation.limit_price * compensation_fee_rate
            )
            compensation.exchange_order_id = f"paper:{compensation.id}"
            compensation.status = "filled"
            compensation.filled_quantity = compensation.quantity
            compensation.average_price = compensation.limit_price
            compensation.updated_at = now
            compensation_fill = FillRow(
                id=str(uuid.uuid4()),
                order_leg_id=compensation.id,
                exchange_trade_id=f"paper:{compensation.id}:fill",
                quantity=compensation.quantity,
                price=compensation.limit_price,
                fee_amount=compensation_fee,
                fee_asset="USDT",
                liquidity="taker",
                occurred_at=now,
            )
            spot_leg = primary["spot"]
            perp_leg = primary["perp"]
            assert spot_leg is not None and perp_leg is not None
            common_quantity = min(
                spot_leg.filled_quantity,
                perp_leg.filled_quantity,
            )
            total_fees = compensation_fee + sum(
                (
                    leg.filled_quantity
                    * leg.limit_price
                    * (intent.spot_fee_rate if leg.market == "spot" else intent.perp_fee_rate)
                )
                for leg in (spot_leg, perp_leg)
            )
            position: PairedPositionRow | None = None
            if common_quantity > 0:
                position = PairedPositionRow(
                    id=str(uuid.uuid4()),
                    opening_intent_id=intent.id,
                    exchange=intent.exchange,
                    environment=intent.environment,
                    base_asset=intent.base_asset,
                    initial_quantity=common_quantity,
                    quantity=common_quantity,
                    spot_entry_price=spot_leg.limit_price,
                    perp_entry_price=perp_leg.limit_price,
                    opening_fees_usdt=total_fees,
                    remaining_opening_fees_usdt=total_fees,
                    status="open",
                    opened_at=now,
                )
                session.add(position)
                intent.status = "hedged"
            else:
                intent.status = "failed"
            intent.version += 1
            intent.updated_at = now
            session.add(compensation_fill)
            await session.commit()
            await session.refresh(intent)
            if position is not None:
                await session.refresh(position)
            return intent, position, True

    async def execute_paper_close(
        self,
        *,
        intent_id: str,
    ) -> tuple[TradeIntentRow, PairedPositionRow, bool] | None:
        value = await self.trade_intent(intent_id)
        if value is None:
            return None
        _, legs = value
        primary = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
        if set(primary) != {"spot", "perp"}:
            return None
        return await self.record_paper_close_fills(
            intent_id=intent_id,
            spot_fill_quantity=primary["spot"].quantity,
            perp_fill_quantity=primary["perp"].quantity,
        )

    async def record_paper_close_fills(
        self,
        *,
        intent_id: str,
        spot_fill_quantity: Decimal,
        perp_fill_quantity: Decimal,
    ) -> tuple[TradeIntentRow, PairedPositionRow, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow).where(TradeIntentRow.id == intent_id).with_for_update()
            )
            if intent is None or intent.paired_position_id is None:
                return None
            position = await session.scalar(
                select(PairedPositionRow)
                .where(PairedPositionRow.id == intent.paired_position_id)
                .with_for_update()
            )
            if position is None:
                return None
            if intent.status in {"closed", "failed"}:
                return intent, position, False
            if (
                intent.environment != "paper"
                or intent.action != "close"
                or intent.status != "planned"
                or position.status != "closing"
                or position.closing_intent_id != intent.id
            ):
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .with_for_update()
                )
            )
            by_leg = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
            if set(by_leg) != {"spot", "perp"}:
                return None
            fill_quantities = {
                "spot": spot_fill_quantity,
                "perp": perp_fill_quantity,
            }
            if any(
                quantity < 0 or quantity > by_leg[leg_name].quantity
                for leg_name, quantity in fill_quantities.items()
            ):
                raise ValueError("paper fill quantity is outside the order quantity")
            now = datetime.now(UTC)
            fills: list[FillRow] = []
            for leg_name, leg in by_leg.items():
                filled_quantity = fill_quantities[leg_name]
                fee_rate = intent.spot_fee_rate if leg_name == "spot" else intent.perp_fee_rate
                fee = filled_quantity * leg.limit_price * fee_rate
                leg.exchange_order_id = f"paper:{leg.id}"
                leg.status = (
                    "filled"
                    if filled_quantity == leg.quantity
                    else "partially_filled"
                    if filled_quantity > 0
                    else "canceled"
                )
                leg.filled_quantity = filled_quantity
                leg.average_price = leg.limit_price if filled_quantity > 0 else None
                leg.updated_at = now
                if filled_quantity > 0:
                    fills.append(
                        FillRow(
                            id=str(uuid.uuid4()),
                            order_leg_id=leg.id,
                            exchange_trade_id=f"paper:{leg.id}:fill",
                            quantity=filled_quantity,
                            price=leg.limit_price,
                            fee_amount=fee,
                            fee_asset="USDT",
                            liquidity="taker",
                            occurred_at=now,
                        )
                    )
            excess_quantity = abs(spot_fill_quantity - perp_fill_quantity)
            if excess_quantity:
                excess_leg = (
                    by_leg["spot"] if spot_fill_quantity > perp_fill_quantity else by_leg["perp"]
                )
                compensation_side = "buy" if excess_leg.side == "sell" else "sell"
                session.add(
                    OrderLegRow(
                        id=str(uuid.uuid4()),
                        trade_intent_id=intent.id,
                        leg=f"{excess_leg.leg}_compensation",
                        market=excess_leg.market,
                        symbol=excess_leg.symbol,
                        side=compensation_side,
                        client_order_id=(f"{excess_leg.client_order_id.rsplit('-', 1)[0]}-c"),
                        status="created",
                        quantity=excess_quantity,
                        limit_price=excess_leg.limit_price,
                        filled_quantity=Decimal("0"),
                        reduce_only=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                intent.status = "compensating"
                intent.version += 1
                intent.updated_at = now
            else:
                session.add(
                    self._apply_paper_close_outcome(
                        intent=intent,
                        position=position,
                        spot_leg=by_leg["spot"],
                        perp_leg=by_leg["perp"],
                        compensation_fee=Decimal("0"),
                        now=now,
                    )
                )
            session.add_all(fills)
            await session.commit()
            await session.refresh(intent)
            await session.refresh(position)
            return intent, position, True

    async def execute_paper_close_compensation(
        self,
        *,
        intent_id: str,
        succeeds: bool = True,
    ) -> tuple[TradeIntentRow, PairedPositionRow, bool] | None:
        async with self.sessions() as session:
            intent = await session.scalar(
                select(TradeIntentRow).where(TradeIntentRow.id == intent_id).with_for_update()
            )
            if intent is None or intent.paired_position_id is None:
                return None
            position = await session.scalar(
                select(PairedPositionRow)
                .where(PairedPositionRow.id == intent.paired_position_id)
                .with_for_update()
            )
            if position is None:
                return None
            if intent.status in {"closed", "failed"}:
                return intent, position, False
            if (
                intent.environment != "paper"
                or intent.action != "close"
                or intent.status != "compensating"
                or position.status != "closing"
                or position.closing_intent_id != intent.id
            ):
                return None
            legs = list(
                await session.scalars(
                    select(OrderLegRow)
                    .where(OrderLegRow.trade_intent_id == intent.id)
                    .with_for_update()
                )
            )
            by_leg = {item.leg: item for item in legs}
            spot_leg = by_leg.get("spot")
            perp_leg = by_leg.get("perp")
            compensations = [
                item for item in legs if item.leg.endswith("_compensation")
            ]
            if spot_leg is None or perp_leg is None or len(compensations) != 1:
                return None
            compensation = compensations[0]
            now = datetime.now(UTC)
            if not succeeds:
                compensation.status = "failed"
                compensation.updated_at = now
                intent.status = "manual_review"
                intent.version += 1
                intent.updated_at = now
                control = await session.get(ExecutionControlRow, 1)
                reason = (
                    "paired trade compensation failed; manual exposure review is required"
                )
                if control is None:
                    session.add(
                        ExecutionControlRow(
                            id=1,
                            state="paused",
                            reason=reason,
                            updated_at=now,
                        )
                    )
                else:
                    control.state = "paused"
                    control.reason = reason
                    control.updated_at = now
                await session.commit()
                await session.refresh(intent)
                await session.refresh(position)
                return intent, position, True

            fee_rate = (
                intent.spot_fee_rate
                if compensation.market == "spot"
                else intent.perp_fee_rate
            )
            compensation_fee = (
                compensation.quantity * compensation.limit_price * fee_rate
            )
            compensation.exchange_order_id = f"paper:{compensation.id}"
            compensation.status = "filled"
            compensation.filled_quantity = compensation.quantity
            compensation.average_price = compensation.limit_price
            compensation.updated_at = now
            session.add(
                FillRow(
                    id=str(uuid.uuid4()),
                    order_leg_id=compensation.id,
                    exchange_trade_id=f"paper:{compensation.id}:fill",
                    quantity=compensation.quantity,
                    price=compensation.limit_price,
                    fee_amount=compensation_fee,
                    fee_asset="USDT",
                    liquidity="taker",
                    occurred_at=now,
                )
            )
            session.add(
                self._apply_paper_close_outcome(
                    intent=intent,
                    position=position,
                    spot_leg=spot_leg,
                    perp_leg=perp_leg,
                    compensation_fee=compensation_fee,
                    now=now,
                )
            )
            await session.commit()
            await session.refresh(intent)
            await session.refresh(position)
            return intent, position, True

    @staticmethod
    def _apply_paper_close_outcome(
        *,
        intent: TradeIntentRow,
        position: PairedPositionRow,
        spot_leg: OrderLegRow,
        perp_leg: OrderLegRow,
        compensation_fee: Decimal,
        now: datetime,
    ) -> PnlRealizationRow:
        common_quantity = min(
            spot_leg.filled_quantity,
            perp_leg.filled_quantity,
        )
        if common_quantity > position.quantity:
            raise ValueError("paper close fill exceeds the remaining position")
        primary_fees = (
            spot_leg.filled_quantity
            * spot_leg.limit_price
            * intent.spot_fee_rate
            + perp_leg.filled_quantity
            * perp_leg.limit_price
            * intent.perp_fee_rate
        )
        attempt_fees = primary_fees + compensation_fee
        opening_fee_allocation = (
            position.remaining_opening_fees_usdt
            if common_quantity == position.quantity
            else (
                position.remaining_opening_fees_usdt
                * common_quantity
                / position.quantity
            )
        )
        gross_pnl = (
            (spot_leg.limit_price - position.spot_entry_price)
            + (position.perp_entry_price - perp_leg.limit_price)
        ) * common_quantity
        net_pnl = gross_pnl - opening_fee_allocation - attempt_fees
        position.closing_fees_usdt = (
            (position.closing_fees_usdt or Decimal("0")) + attempt_fees
        )
        position.realized_pnl_usdt = (
            (position.realized_pnl_usdt or Decimal("0"))
            + net_pnl
        )
        position.remaining_opening_fees_usdt -= opening_fee_allocation
        position.quantity -= common_quantity
        if position.quantity == 0:
            position.status = "closed"
            position.closed_at = now
            intent.status = "closed"
        else:
            position.status = "open"
            position.closing_intent_id = None
            intent.status = "closed" if common_quantity > 0 else "failed"
        intent.version += 1
        intent.updated_at = now
        return PnlRealizationRow(
            id=str(uuid.uuid4()),
            paired_position_id=position.id,
            closing_intent_id=intent.id,
            quantity=common_quantity,
            gross_pnl_usdt=gross_pnl,
            opening_fee_allocated_usdt=opening_fee_allocation,
            closing_fees_usdt=attempt_fees,
            net_pnl_usdt=net_pnl,
            realized_at=now,
        )

    async def daily_realized_pnl(
        self,
        *,
        environment: str,
        exchanges: set[str],
        since: datetime,
    ) -> Decimal:
        if not exchanges:
            return Decimal("0")
        async with self.sessions() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(PnlRealizationRow.net_pnl_usdt), 0))
                .join(
                    PairedPositionRow,
                    PnlRealizationRow.paired_position_id
                    == PairedPositionRow.id,
                )
                .where(
                    PairedPositionRow.environment == environment,
                    PairedPositionRow.exchange.in_(exchanges),
                    PnlRealizationRow.realized_at >= _utc(since),
                )
            )
            return Decimal(value or 0)

    async def list_paired_positions(self, *, status: str | None = None) -> list[PairedPositionRow]:
        async with self.sessions() as session:
            statement = select(PairedPositionRow)
            if status is not None:
                statement = statement.where(PairedPositionRow.status == status)
            return list(
                await session.scalars(statement.order_by(PairedPositionRow.opened_at.desc()))
            )

    async def active_open_intent_keys(
        self,
        *,
        environment: str,
    ) -> set[str]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(
                    TradeIntentRow.exchange,
                    TradeIntentRow.base_asset,
                ).where(
                    TradeIntentRow.environment == environment,
                    TradeIntentRow.action == "open",
                    TradeIntentRow.status.in_(
                        {
                            "planned",
                            "executing",
                            "compensating",
                            "manual_review",
                        }
                    ),
                )
            )
            return {
                f"{exchange}:{base_asset}"
                for exchange, base_asset in rows
            }

    async def position_liquidation_buffers(
        self,
        *,
        environment: str,
        exchanges: set[str],
    ) -> dict[str, Decimal | None]:
        if not exchanges:
            return {}
        async with self.sessions() as session:
            rows = await session.execute(
                select(
                    PairedPositionRow.id,
                    RemotePositionSnapshotRow.mark_price,
                    RemotePositionSnapshotRow.liquidation_price,
                )
                .join(
                    TradeIntentRow,
                    PairedPositionRow.opening_intent_id
                    == TradeIntentRow.id,
                )
                .join(
                    OrderLegRow,
                    (OrderLegRow.trade_intent_id == TradeIntentRow.id)
                    & (OrderLegRow.leg == "perp"),
                )
                .join(
                    AccountReconciliationRow,
                    (
                        AccountReconciliationRow.exchange
                        == PairedPositionRow.exchange
                    )
                    & (
                        AccountReconciliationRow.environment
                        == PairedPositionRow.environment
                    ),
                )
                .join(
                    RemotePositionSnapshotRow,
                    (
                        RemotePositionSnapshotRow.account_snapshot_id
                        == AccountReconciliationRow.snapshot_id
                    )
                    & (
                        RemotePositionSnapshotRow.symbol
                        == OrderLegRow.symbol
                    ),
                )
                .where(
                    PairedPositionRow.environment == environment,
                    PairedPositionRow.exchange.in_(exchanges),
                    PairedPositionRow.status.in_({"open", "closing"}),
                )
            )
            values: dict[str, Decimal | None] = {}
            for position_id, mark_price, liquidation_price in rows:
                if (
                    mark_price <= 0
                    or liquidation_price is None
                    or liquidation_price <= 0
                ):
                    values[position_id] = None
                else:
                    values[position_id] = (
                        liquidation_price - mark_price
                    ) / mark_price
            return values
    async def paired_perp_exposures(
        self,
        *,
        exchange: str,
        environment: str,
    ) -> list[tuple[str, Decimal, int]]:
        async with self.sessions() as session:
            rows = list(
                await session.execute(
                    select(
                        PairedPositionRow,
                        OrderLegRow,
                        TradeIntentRow,
                    )
                    .join(
                        TradeIntentRow,
                        PairedPositionRow.opening_intent_id
                        == TradeIntentRow.id,
                    )
                    .join(
                        OrderLegRow,
                        OrderLegRow.trade_intent_id == TradeIntentRow.id,
                    )
                    .where(
                        PairedPositionRow.exchange == exchange,
                        PairedPositionRow.environment == environment,
                        PairedPositionRow.status.in_({"open", "closing"}),
                        OrderLegRow.leg == "perp",
                    )
                )
            )
            return [
                (
                    leg.symbol,
                    position.quantity / leg.base_multiplier,
                    intent.leverage,
                )
                for position, leg, intent in rows
            ]

    async def fills_for_intent(self, intent_id: str) -> list[FillRow]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(FillRow)
                    .join(OrderLegRow, FillRow.order_leg_id == OrderLegRow.id)
                    .where(OrderLegRow.trade_intent_id == intent_id)
                    .order_by(FillRow.occurred_at, FillRow.id)
                )
            )

    async def load_settings(self) -> ScannerSettings:
        async with self.sessions() as session:
            row = await session.get(SettingRow, "scanner")
            return ScannerSettings.model_validate_json(row.payload) if row else ScannerSettings()

    async def save_settings(self, settings: ScannerSettings) -> None:
        async with self.sessions() as session:
            row = await session.get(SettingRow, "scanner")
            payload = settings.model_dump_json()
            if row:
                row.payload = payload
            else:
                session.add(SettingRow(key="scanner", payload=payload))
            await session.commit()

    async def admin_count(self) -> int:
        async with self.sessions() as session:
            return int(await session.scalar(select(func.count(AdminUserRow.id))) or 0)

    async def create_admin(
        self,
        *,
        username: str,
        password_hash: str,
        totp_ciphertext: str,
        totp_nonce: str,
        key_version: int,
    ) -> AdminUserRow:
        async with self.sessions() as session:
            row = AdminUserRow(
                username=username,
                password_hash=password_hash,
                totp_ciphertext=totp_ciphertext,
                totp_nonce=totp_nonce,
                key_version=key_version,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get_admin_by_username(self, username: str) -> AdminUserRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(AdminUserRow).where(AdminUserRow.username == username)
            )

    async def create_session(
        self,
        *,
        admin_id: int,
        token_hash: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> None:
        async with self.sessions() as session:
            session.add(
                AdminSessionRow(
                    token_hash=token_hash,
                    admin_id=admin_id,
                    csrf_hash=csrf_hash,
                    created_at=datetime.now(UTC),
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def admin_for_session(self, *, token_hash: str, now: datetime) -> AdminUserRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(AdminUserRow)
                .join(AdminSessionRow, AdminSessionRow.admin_id == AdminUserRow.id)
                .where(
                    AdminSessionRow.token_hash == token_hash,
                    AdminSessionRow.expires_at > now,
                )
            )

    async def csrf_hash_for_session(self, *, token_hash: str, now: datetime) -> str | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(AdminSessionRow.csrf_hash).where(
                    AdminSessionRow.token_hash == token_hash,
                    AdminSessionRow.expires_at > now,
                )
            )

    async def delete_session(self, token_hash: str) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(AdminSessionRow).where(AdminSessionRow.token_hash == token_hash)
            )
            await session.commit()

    async def append_audit(self, event_type: str, *, actor: str, details: dict[str, Any]) -> None:
        async with self.sessions() as session:
            session.add(
                AuditEventRow(
                    id=str(uuid.uuid4()),
                    occurred_at=datetime.now(UTC),
                    event_type=event_type,
                    actor=actor,
                    details=json.dumps(details, separators=(",", ":"), sort_keys=True),
                )
            )
            await session.commit()

    async def enqueue_notification(
        self,
        *,
        dedupe_key: str,
        event_type: str,
        severity: str,
        channels: set[str],
        subject: str,
        body: str,
        now: datetime | None = None,
    ) -> list[NotificationOutboxItem]:
        if not dedupe_key or len(dedupe_key) > 200:
            raise ValueError("notification dedupe key must contain 1-200 characters")
        if not event_type or len(event_type) > 100:
            raise ValueError("notification event type must contain 1-100 characters")
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("unsupported notification severity")
        if not channels or not channels <= {"telegram", "email"}:
            raise ValueError("unsupported notification channel")
        if not subject or len(subject) > 200:
            raise ValueError("notification subject must contain 1-200 characters")
        if not body:
            raise ValueError("notification body is required")
        created_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            for channel in sorted(channels):
                try:
                    async with session.begin_nested():
                        session.add(
                            NotificationOutboxRow(
                                id=str(uuid.uuid4()),
                                dedupe_key=dedupe_key,
                                event_type=event_type,
                                severity=severity,
                                channel=channel,
                                subject=subject,
                                body=body,
                                status="pending",
                                attempts=0,
                                next_attempt_at=created_at,
                                created_at=created_at,
                                updated_at=created_at,
                            )
                        )
                        await session.flush()
                except IntegrityError:
                    pass
            await session.commit()
            rows = list(
                await session.scalars(
                    select(NotificationOutboxRow)
                    .where(
                        NotificationOutboxRow.dedupe_key == dedupe_key,
                        NotificationOutboxRow.channel.in_(channels),
                    )
                    .order_by(NotificationOutboxRow.channel)
                )
            )
            return [_notification_item(row) for row in rows]

    async def claim_notifications(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=5),
    ) -> list[NotificationOutboxItem]:
        if limit < 1 or limit > 100:
            raise ValueError("notification claim limit must be between 1 and 100")
        claimed_at = now or datetime.now(UTC)
        stale_before = claimed_at - stale_after
        async with self.sessions() as session:
            statement = (
                select(NotificationOutboxRow)
                .where(
                    (
                        NotificationOutboxRow.status.in_({"pending", "retry"})
                        & (NotificationOutboxRow.next_attempt_at <= claimed_at)
                    )
                    | (
                        (NotificationOutboxRow.status == "sending")
                        & (NotificationOutboxRow.updated_at <= stale_before)
                    )
                )
                .order_by(
                    NotificationOutboxRow.next_attempt_at,
                    NotificationOutboxRow.created_at,
                )
                .limit(limit)
            )
            if self.engine.url.get_backend_name() == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = list(await session.scalars(statement))
            for row in rows:
                row.status = "sending"
                row.attempts += 1
                row.updated_at = claimed_at
                row.last_error_code = None
            await session.commit()
            return [_notification_item(row) for row in rows]

    async def mark_notification_sent(
        self,
        notification_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        sent_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            result = await session.execute(
                update(NotificationOutboxRow)
                .where(
                    NotificationOutboxRow.id == notification_id,
                    NotificationOutboxRow.status == "sending",
                )
                .values(
                    status="sent",
                    updated_at=sent_at,
                    sent_at=sent_at,
                    last_error_code=None,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_notification_failed(
        self,
        notification_id: str,
        *,
        error_code: str,
        now: datetime | None = None,
        max_attempts: int = 8,
    ) -> NotificationOutboxItem | None:
        if max_attempts < 1:
            raise ValueError("notification max attempts must be positive")
        if (
            not error_code
            or len(error_code) > 80
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in error_code
            )
        ):
            raise ValueError("notification error code must be a safe lowercase identifier")
        failed_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            statement = select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == notification_id
            )
            if self.engine.url.get_backend_name() == "postgresql":
                statement = statement.with_for_update()
            row = await session.scalar(statement)
            if row is None or row.status != "sending":
                return None
            if row.attempts >= max_attempts:
                row.status = "dead"
                row.next_attempt_at = failed_at
            else:
                delay_seconds = min(30 * (2 ** (row.attempts - 1)), 3600)
                row.status = "retry"
                row.next_attempt_at = failed_at + timedelta(seconds=delay_seconds)
            row.last_error_code = error_code
            row.updated_at = failed_at
            await session.commit()
            return _notification_item(row)

    async def notification_outbox(
        self,
        *,
        limit: int = 100,
    ) -> list[NotificationOutboxItem]:
        if limit < 1 or limit > 500:
            raise ValueError("notification list limit must be between 1 and 500")
        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(NotificationOutboxRow)
                    .order_by(NotificationOutboxRow.created_at.desc())
                    .limit(limit)
                )
            )
            return [_notification_item(row) for row in rows]

    async def project_notification(
        self,
        *,
        source_key: str,
        fingerprint: str,
        notify: bool,
        event_type: str,
        severity: str,
        channels: set[str],
        subject: str,
        body: str,
        now: datetime | None = None,
    ) -> bool:
        if not source_key or len(source_key) > 150:
            raise ValueError("notification source key must contain 1-150 characters")
        if (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("notification fingerprint must be lowercase SHA-256")
        projected_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            statement = select(NotificationProjectionStateRow).where(
                NotificationProjectionStateRow.source_key == source_key
            )
            if self.engine.url.get_backend_name() == "postgresql":
                statement = statement.with_for_update()
            state = await session.scalar(statement)
            if state is not None and state.fingerprint == fingerprint:
                return False
            generation = (state.generation if state is not None else 0) + 1
            if state is None:
                state = NotificationProjectionStateRow(
                    source_key=source_key,
                    fingerprint=fingerprint,
                    generation=generation,
                    updated_at=projected_at,
                )
                session.add(state)
            else:
                state.fingerprint = fingerprint
                state.generation = generation
                state.updated_at = projected_at
            if notify:
                if severity not in {"info", "warning", "critical"}:
                    raise ValueError("unsupported notification severity")
                if not channels or not channels <= {"telegram", "email"}:
                    raise ValueError("unsupported notification channel")
                if not event_type or len(event_type) > 100:
                    raise ValueError(
                        "notification event type must contain 1-100 characters"
                    )
                if not subject or len(subject) > 200:
                    raise ValueError(
                        "notification subject must contain 1-200 characters"
                    )
                if not body:
                    raise ValueError("notification body is required")
                dedupe_key = f"projection:{source_key}:{generation}"
                session.add_all(
                    NotificationOutboxRow(
                        id=str(uuid.uuid4()),
                        dedupe_key=dedupe_key,
                        event_type=event_type,
                        severity=severity,
                        channel=channel,
                        subject=subject,
                        body=body,
                        status="pending",
                        attempts=0,
                        next_attempt_at=projected_at,
                        created_at=projected_at,
                        updated_at=projected_at,
                    )
                    for channel in sorted(channels)
                )
            await session.commit()
            return notify

    async def notification_trade_intents(
        self,
        *,
        updated_since: datetime,
    ) -> list[TradeIntentRow]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(TradeIntentRow)
                    .where(
                        (TradeIntentRow.updated_at >= updated_since)
                        | TradeIntentRow.status.in_(
                            {
                                "planned",
                                "executing",
                                "compensating",
                                "manual_review",
                            }
                        )
                    )
                    .order_by(TradeIntentRow.created_at)
                )
            )

    async def notification_daily_summary(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> DailyNotificationSummary:
        start = _utc(period_start)
        end = _utc(period_end)
        if end <= start:
            raise ValueError("notification summary period must be positive")
        async with self.sessions() as session:
            realized = await session.execute(
                select(
                    func.count(PnlRealizationRow.id),
                    func.coalesce(
                        func.sum(PnlRealizationRow.net_pnl_usdt),
                        0,
                    ),
                ).where(
                    PnlRealizationRow.realized_at >= start,
                    PnlRealizationRow.realized_at < end,
                )
            )
            realized_count, realized_pnl = realized.one()
            opened_count = await session.scalar(
                select(func.count(TradeIntentRow.id)).where(
                    TradeIntentRow.action == "open",
                    TradeIntentRow.status == "hedged",
                    TradeIntentRow.updated_at >= start,
                    TradeIntentRow.updated_at < end,
                )
            )
            closed_count = await session.scalar(
                select(func.count(TradeIntentRow.id)).where(
                    TradeIntentRow.action == "close",
                    TradeIntentRow.status == "closed",
                    TradeIntentRow.updated_at >= start,
                    TradeIntentRow.updated_at < end,
                )
            )
            failed_count = await session.scalar(
                select(func.count(TradeIntentRow.id)).where(
                    TradeIntentRow.status.in_({"failed", "manual_review"}),
                    TradeIntentRow.updated_at >= start,
                    TradeIntentRow.updated_at < end,
                )
            )
            active_count = await session.scalar(
                select(func.count(PairedPositionRow.id)).where(
                    PairedPositionRow.status.in_({"open", "closing"})
                )
            )
            unhealthy_count = await session.scalar(
                select(func.count()).select_from(AccountReconciliationRow).where(
                    AccountReconciliationRow.status != "ready"
                )
            )
            return DailyNotificationSummary(
                period_start=start,
                period_end=end,
                realized_event_count=int(realized_count or 0),
                realized_net_pnl_usdt=Decimal(realized_pnl or 0),
                opened_trade_count=int(opened_count or 0),
                closed_trade_count=int(closed_count or 0),
                failed_trade_count=int(failed_count or 0),
                active_position_count=int(active_count or 0),
                unhealthy_account_count=int(unhealthy_count or 0),
            )

    async def plan_internal_transfer(
        self,
        *,
        transfer_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        exchange: str,
        environment: str,
        direction: str,
        amount: Decimal,
        per_request_limit: Decimal,
        daily_limit: Decimal,
        actor: str,
        now: datetime | None = None,
    ) -> tuple[InternalTransferRow, bool]:
        if direction not in {"spot_to_perp", "perp_to_spot"}:
            raise ValueError("unsupported internal transfer direction")
        if environment not in {"sandbox", "live"}:
            raise ValueError("internal transfer environment must be sandbox or live")
        if amount <= 0:
            raise ValueError("internal transfer amount must be positive")
        if per_request_limit <= 0 or daily_limit <= 0:
            raise ValueError("internal transfers are disabled by zero limits")
        if amount > per_request_limit:
            raise ValueError("internal transfer exceeds the per-request limit")
        if per_request_limit > daily_limit:
            raise ValueError("per-request transfer limit cannot exceed daily limit")
        observed_at = now or datetime.now(UTC)
        day_start = observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.sessions() as session:
            existing = await session.scalar(
                select(InternalTransferRow).where(
                    InternalTransferRow.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ValueError(
                        "internal transfer idempotency key conflicts with another request"
                    )
                return existing, False
            control = await session.scalar(
                select(ExecutionControlRow)
                .where(ExecutionControlRow.id == 1)
                .with_for_update()
            )
            if control is None:
                control = ExecutionControlRow(
                    id=1,
                    state="blocked",
                    reason="execution worker has not completed startup reconciliation",
                    updated_at=observed_at,
                )
                session.add(control)
                await session.flush()
            used = await session.scalar(
                select(
                    func.coalesce(func.sum(InternalTransferRow.amount), 0)
                ).where(
                    InternalTransferRow.created_at >= day_start,
                    InternalTransferRow.created_at < day_start
                    + timedelta(days=1),
                    InternalTransferRow.status.not_in({"failed"}),
                )
            )
            if Decimal(used or 0) + amount > daily_limit:
                raise ValueError("internal transfer exceeds the UTC daily limit")
            row = InternalTransferRow(
                id=transfer_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                exchange=exchange,
                environment=environment,
                asset="USDT",
                direction=direction,
                amount=amount,
                status="planned",
                created_at=observed_at,
                updated_at=observed_at,
            )
            session.add(row)
            if control.state != "paused":
                control.state = "paused"
                control.reason = (
                    "internal account transfer requires balance confirmation"
                )
                control.updated_at = observed_at
            session.add(
                AuditEventRow(
                    id=str(uuid.uuid4()),
                    occurred_at=observed_at,
                    event_type="transfer.planned",
                    actor=actor,
                    details=json.dumps(
                        {
                            "transfer_id": transfer_id,
                            "exchange": exchange,
                            "environment": environment,
                            "asset": "USDT",
                            "direction": direction,
                            "amount": format(amount, "f"),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
            await session.refresh(row)
            return row, True

    async def list_internal_transfers(
        self,
        *,
        limit: int = 100,
    ) -> list[InternalTransferRow]:
        if limit < 1 or limit > 500:
            raise ValueError("internal transfer list limit must be between 1 and 500")
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(InternalTransferRow)
                    .order_by(InternalTransferRow.created_at.desc())
                    .limit(limit)
                )
            )

    async def next_internal_transfer(
        self,
        *,
        statuses: set[str],
    ) -> InternalTransferRow | None:
        allowed = {"planned", "submitted", "pending"}
        if not statuses or not statuses.issubset(allowed):
            raise ValueError("unsupported active internal transfer status")
        async with self.sessions() as session:
            return await session.scalar(
                select(InternalTransferRow)
                .where(InternalTransferRow.status.in_(statuses))
                .order_by(InternalTransferRow.created_at.asc())
                .limit(1)
            )

    async def internal_transfer_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> InternalTransferRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(InternalTransferRow).where(
                    InternalTransferRow.idempotency_key == idempotency_key
                )
            )

    async def prepare_internal_transfer_submission(
        self,
        *,
        transfer_id: str,
        source_balance: Decimal,
        target_balance: Decimal,
        now: datetime | None = None,
    ) -> InternalTransferRow | None:
        if source_balance < 0 or target_balance < 0:
            raise ValueError("internal transfer balances cannot be negative")
        observed_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            control = await session.scalar(
                select(ExecutionControlRow)
                .where(ExecutionControlRow.id == 1)
                .with_for_update()
            )
            row = await session.scalar(
                select(InternalTransferRow)
                .where(InternalTransferRow.id == transfer_id)
                .with_for_update()
            )
            if row is None or row.status != "planned":
                return None
            if control is None or control.state != "paused":
                raise ValueError(
                    "internal transfer submission requires paused execution"
                )
            if source_balance < row.amount:
                raise ValueError("internal transfer source balance is insufficient")
            row.source_balance_before = source_balance
            row.target_balance_before = target_balance
            row.expected_target_balance = target_balance + row.amount
            row.status = "submitted"
            row.submitted_at = observed_at
            row.updated_at = observed_at
            session.add(
                AuditEventRow(
                    id=str(uuid.uuid4()),
                    occurred_at=observed_at,
                    event_type="transfer.submission_started",
                    actor="worker",
                    details=json.dumps(
                        {
                            "transfer_id": row.id,
                            "exchange": row.exchange,
                            "environment": row.environment,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def record_internal_transfer_remote_id(
        self,
        *,
        transfer_id: str,
        exchange_transfer_id: str,
        now: datetime | None = None,
    ) -> InternalTransferRow:
        if not exchange_transfer_id:
            raise ValueError("exchange transfer ID is required")
        observed_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            row = await session.scalar(
                select(InternalTransferRow)
                .where(InternalTransferRow.id == transfer_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("internal transfer does not exist")
            if row.status not in {"submitted", "pending"}:
                raise ValueError("internal transfer is not awaiting remote confirmation")
            if (
                row.exchange_transfer_id is not None
                and row.exchange_transfer_id != exchange_transfer_id
            ):
                raise ValueError("exchange transfer ID conflicts with persisted value")
            row.exchange_transfer_id = exchange_transfer_id
            row.status = "pending"
            row.error_code = None
            row.updated_at = observed_at
            await session.commit()
            await session.refresh(row)
            return row

    async def finalize_internal_transfer(
        self,
        *,
        transfer_id: str,
        status: str,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> InternalTransferRow:
        if status not in {"completed", "failed", "manual_review"}:
            raise ValueError("unsupported internal transfer terminal status")
        if error_code is not None and (
            not error_code or len(error_code) > 80
        ):
            raise ValueError("invalid internal transfer error code")
        observed_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            row = await session.scalar(
                select(InternalTransferRow)
                .where(InternalTransferRow.id == transfer_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("internal transfer does not exist")
            if row.status in {"completed", "failed", "manual_review"}:
                if row.status != status:
                    raise ValueError("internal transfer already has another terminal status")
                return row
            row.status = status
            row.error_code = error_code
            row.updated_at = observed_at
            if status == "completed":
                row.completed_at = observed_at
            session.add(
                AuditEventRow(
                    id=str(uuid.uuid4()),
                    occurred_at=observed_at,
                    event_type=f"transfer.{status}",
                    actor="worker",
                    details=json.dumps(
                        {
                            "transfer_id": row.id,
                            "exchange": row.exchange,
                            "environment": row.environment,
                            "error_code": error_code,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def save_exchange_credential(
        self,
        *,
        exchange: str,
        environment: str,
        label: str,
        masked_api_key: str,
        ciphertext: str,
        nonce: str,
        key_version: int,
    ) -> ExchangeCredentialRow:
        async with self.sessions() as session:
            row = await session.scalar(
                select(ExchangeCredentialRow).where(
                    ExchangeCredentialRow.exchange == exchange,
                    ExchangeCredentialRow.environment == environment,
                )
            )
            now = datetime.now(UTC)
            if row:
                row.label = label
                row.masked_api_key = masked_api_key
                row.ciphertext = ciphertext
                row.nonce = nonce
                row.key_version = key_version
                row.updated_at = now
            else:
                row = ExchangeCredentialRow(
                    id=str(uuid.uuid4()),
                    exchange=exchange,
                    environment=environment,
                    label=label,
                    masked_api_key=masked_api_key,
                    ciphertext=ciphertext,
                    nonce=nonce,
                    key_version=key_version,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def exchange_credential(
        self, exchange: str, environment: str
    ) -> ExchangeCredentialRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(ExchangeCredentialRow).where(
                    ExchangeCredentialRow.exchange == exchange,
                    ExchangeCredentialRow.environment == environment,
                )
            )

    async def list_exchange_credentials(self) -> list[ExchangeCredentialRow]:
        async with self.sessions() as session:
            values = await session.scalars(
                select(ExchangeCredentialRow).order_by(
                    ExchangeCredentialRow.exchange,
                    ExchangeCredentialRow.environment,
                )
            )
            return list(values)

    async def delete_exchange_credential(self, exchange: str, environment: str) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                delete(ExchangeCredentialRow).where(
                    ExchangeCredentialRow.exchange == exchange,
                    ExchangeCredentialRow.environment == environment,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def replace_instruments(self, exchange: str, pairs: list[InstrumentPair]) -> None:
        async with self.sessions() as session:
            await session.execute(delete(InstrumentRow).where(InstrumentRow.exchange == exchange))
            now = datetime.now(UTC)
            session.add_all(
                InstrumentRow(
                    key=pair.key,
                    exchange=pair.exchange.value,
                    base_asset=pair.base_asset,
                    spot_symbol=pair.spot_symbol,
                    perp_symbol=pair.perp_symbol,
                    interval_hours=str(pair.funding_interval_hours),
                    spot_price_increment=pair.spot_price_increment,
                    spot_quantity_increment=pair.spot_quantity_increment,
                    spot_min_quantity=pair.spot_min_quantity,
                    spot_min_notional=pair.spot_min_notional,
                    perp_price_increment=pair.perp_price_increment,
                    perp_quantity_increment=pair.perp_quantity_increment,
                    perp_min_quantity=pair.perp_min_quantity,
                    perp_min_notional=pair.perp_min_notional,
                    perp_contract_size=pair.perp_contract_size,
                    updated_at=now,
                )
                for pair in pairs
            )
            await session.commit()

    async def instrument_pairs(
        self,
        *,
        exchanges: set[str] | None = None,
    ) -> list[InstrumentPair]:
        async with self.sessions() as session:
            statement = select(InstrumentRow)
            if exchanges is not None:
                statement = statement.where(
                    InstrumentRow.exchange.in_(exchanges)
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        InstrumentRow.exchange,
                        InstrumentRow.base_asset,
                    )
                )
            )
        return [
            InstrumentPair(
                exchange=row.exchange,
                base_asset=row.base_asset,
                spot_symbol=row.spot_symbol,
                perp_symbol=row.perp_symbol,
                funding_interval_hours=Decimal(row.interval_hours),
                spot_price_increment=row.spot_price_increment,
                spot_quantity_increment=row.spot_quantity_increment,
                spot_min_quantity=row.spot_min_quantity,
                spot_min_notional=row.spot_min_notional,
                perp_price_increment=row.perp_price_increment,
                perp_quantity_increment=row.perp_quantity_increment,
                perp_min_quantity=row.perp_min_quantity,
                perp_min_notional=row.perp_min_notional,
                perp_contract_size=row.perp_contract_size,
            )
            for row in rows
        ]

    async def save_latest_opportunities(
        self,
        opportunities: list[Opportunity],
    ) -> None:
        if not opportunities:
            return
        async with self.sessions() as session:
            now = datetime.now(UTC)
            for item in opportunities:
                row = await session.get(LatestOpportunityRow, item.key)
                payload = item.model_dump_json()
                if row is None:
                    session.add(
                        LatestOpportunityRow(
                            key=item.key,
                            exchange=item.exchange.value,
                            base_asset=item.base_asset,
                            observed_at=item.observed_at,
                            payload=payload,
                            updated_at=now,
                        )
                    )
                else:
                    row.exchange = item.exchange.value
                    row.base_asset = item.base_asset
                    row.observed_at = item.observed_at
                    row.payload = payload
                    row.updated_at = now
            await session.commit()

    async def latest_opportunities(
        self,
        *,
        exchanges: set[str] | None = None,
    ) -> list[Opportunity]:
        async with self.sessions() as session:
            statement = select(LatestOpportunityRow)
            if exchanges is not None:
                statement = statement.where(
                    LatestOpportunityRow.exchange.in_(exchanges)
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        LatestOpportunityRow.exchange,
                        LatestOpportunityRow.base_asset,
                    )
                )
            )
        return [
            Opportunity.model_validate_json(row.payload)
            for row in rows
        ]

    async def save_funding(self, observations: list[FundingObservation]) -> None:
        if not observations:
            return
        async with self.sessions() as session:
            for item in observations:
                existing = await session.scalar(
                    select(FundingRow.id).where(
                        FundingRow.exchange == item.exchange.value,
                        FundingRow.base_asset == item.base_asset,
                        FundingRow.funding_at == item.funding_at,
                    )
                )
                if existing is None:
                    session.add(
                        FundingRow(
                            exchange=item.exchange.value,
                            base_asset=item.base_asset,
                            funding_at=item.funding_at,
                            rate=str(item.rate),
                            interval_hours=str(item.interval_hours),
                        )
                    )
            await session.commit()

    async def funding_history(
        self, exchange: str, base_asset: str, *, since: datetime
    ) -> list[FundingObservation]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(FundingRow)
                    .where(
                        FundingRow.exchange == exchange,
                        FundingRow.base_asset == base_asset,
                        FundingRow.funding_at >= since,
                    )
                    .order_by(FundingRow.funding_at)
                )
            ).all()
        return [
            FundingObservation(
                exchange=exchange,
                base_asset=base_asset,
                rate=Decimal(row.rate),
                funding_at=_utc(row.funding_at),
                observed_at=_utc(row.funding_at),
                settled=True,
                interval_hours=Decimal(row.interval_hours),
            )
            for row in rows
        ]

    async def save_snapshots(self, opportunities: list[Opportunity]) -> None:
        async with self.sessions() as session:
            session.add_all(
                SnapshotRow(
                    exchange=item.exchange.value,
                    base_asset=item.base_asset,
                    observed_at=item.observed_at.replace(second=0, microsecond=0),
                    payload=item.model_dump_json(),
                )
                for item in opportunities
            )
            await session.commit()

    async def snapshot_history(
        self, exchange: str, base_asset: str, *, since: datetime
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(SnapshotRow)
                    .where(
                        SnapshotRow.exchange == exchange,
                        SnapshotRow.base_asset == base_asset,
                        SnapshotRow.observed_at >= since,
                    )
                    .order_by(SnapshotRow.observed_at)
                )
            ).all()
        return [json.loads(row.payload) for row in rows]

    async def prune(self, retention_days: int, *, batch_size: int = 5000) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.sessions() as session:
            ids = (
                await session.scalars(
                    select(SnapshotRow.id).where(SnapshotRow.observed_at < cutoff).limit(batch_size)
                )
            ).all()
            if ids:
                await session.execute(delete(SnapshotRow).where(SnapshotRow.id.in_(ids)))
            await session.commit()
            return len(ids)
