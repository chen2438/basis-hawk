from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
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
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from basis_hawk.models import FundingObservation, InstrumentPair, Opportunity, ScannerSettings

if TYPE_CHECKING:
    from basis_hawk.accounts import RemoteFill, RemoteOrder


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
    open_order_count: Mapped[int] = mapped_column(Integer, default=0)
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, default=0)
    recovered_order_count: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    status: Mapped[str] = mapped_column(String(30), index=True)
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
    __table_args__ = (UniqueConstraint("trade_intent_id", "leg", name="uq_order_leg_intent_leg"),)


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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _numeric_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.000000000000001")


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

    async def reconciliation_states(self) -> list[AccountReconciliationRow]:
        async with self.sessions() as session:
            values = await session.scalars(
                select(AccountReconciliationRow).order_by(
                    AccountReconciliationRow.exchange,
                    AccountReconciliationRow.environment,
                )
            )
            return list(values)

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
            elif filled_quantity > 0:
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
            if order.filled_quantity == 0 and remote_status in {
                "cancelled",
                "canceled",
                "deactivated",
                "4",
            }:
                leg.status = "canceled"
            elif order.filled_quantity == 0 and remote_status in {
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
                self._apply_paper_close_outcome(
                    intent=intent,
                    position=position,
                    spot_leg=by_leg["spot"],
                    perp_leg=by_leg["perp"],
                    compensation_fee=Decimal("0"),
                    now=now,
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
            self._apply_paper_close_outcome(
                intent=intent,
                position=position,
                spot_leg=spot_leg,
                perp_leg=perp_leg,
                compensation_fee=compensation_fee,
                now=now,
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
    ) -> None:
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
        position.closing_fees_usdt = (
            (position.closing_fees_usdt or Decimal("0")) + attempt_fees
        )
        position.realized_pnl_usdt = (
            (position.realized_pnl_usdt or Decimal("0"))
            + gross_pnl
            - opening_fee_allocation
            - attempt_fees
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

    async def list_paired_positions(self, *, status: str | None = None) -> list[PairedPositionRow]:
        async with self.sessions() as session:
            statement = select(PairedPositionRow)
            if status is not None:
                statement = statement.where(PairedPositionRow.status == status)
            return list(
                await session.scalars(statement.order_by(PairedPositionRow.opened_at.desc()))
            )

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
                    updated_at=now,
                )
                for pair in pairs
            )
            await session.commit()

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
