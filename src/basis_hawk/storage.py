from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
)
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from basis_hawk.models import FundingObservation, InstrumentPair, Opportunity, ScannerSettings


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
    __table_args__ = (
        UniqueConstraint("exchange", "environment", name="uq_exchange_credential"),
    )


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
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionControlRow(Base):
    __tablename__ = "execution_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
                    checked_at=checked_at,
                )
                session.add(row)
            else:
                row.status = status
                row.reason = reason
                row.snapshot_id = snapshot_id
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

    async def admin_for_session(
        self, *, token_hash: str, now: datetime
    ) -> AdminUserRow | None:
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

    async def append_audit(
        self, event_type: str, *, actor: str, details: dict[str, Any]
    ) -> None:
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
