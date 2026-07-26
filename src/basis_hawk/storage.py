from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
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
