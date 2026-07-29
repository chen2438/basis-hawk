from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import PrivateAccountClient
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.storage import Database


class AdlPositionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    account_label: str
    exchange: Exchange
    environment: ExchangeEnvironment
    symbol: str
    position_side: str
    risk_level: int | None
    native_value: str | None
    event_only: bool
    observed_at: datetime


AccountClientFactory = Callable[
    [Exchange, ExchangeSecrets, ExchangeEnvironment],
    PrivateAccountClient,
]


class AdlMonitorService:
    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        account_client_factory: AccountClientFactory,
    ) -> None:
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory

    async def list(self) -> list[AdlPositionView]:
        return [
            AdlPositionView(
                account_id=row.account_id,
                account_label=account.label,
                exchange=Exchange(account.exchange),
                environment=ExchangeEnvironment(account.environment),
                symbol=row.symbol,
                position_side=row.position_side,
                risk_level=row.normalized_level,
                native_value=row.native_value,
                event_only=row.event_only,
                observed_at=row.observed_at,
            )
            for row, account in await self.database.latest_adl_snapshots()
        ]

    async def refresh(self) -> list[AdlPositionView]:
        for account in await self.credentials.list():
            secrets = await self.credentials.load_by_id(account.id)
            if secrets is None:
                continue
            client = self.account_client_factory(
                account.exchange,
                secrets,
                account.environment,
            )
            try:
                batch = await client.adl_ranks()
            finally:
                await client.close()
            observed_at = max(
                (item.observed_at for item in batch.positions),
                default=datetime.now(UTC),
            )
            await self.database.save_adl_snapshot_batch(
                account_id=account.id,
                positions=[
                    {
                        "symbol": item.symbol,
                        "position_side": item.position_side,
                        "normalized_level": item.risk_level,
                        "native_value": item.native_value,
                    }
                    for item in batch.positions
                ],
                event_only=batch.event_only,
                observed_at=observed_at,
            )
        return await self.list()
