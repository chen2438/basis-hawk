from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, delete, select, text
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
