from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import (
    PositionMode,
    PrivateAccountClient,
    create_account_client,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.storage import Database
from basis_hawk.trading import PaperExecutionService


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accounts_checked: int
    accounts_blocked: int
    accounts_failed: int
    execution_state: str


class WorkerLockUnavailable(RuntimeError):
    pass


class ReconciliationService:
    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        *,
        account_client_factory: Callable[
            [Exchange, ExchangeSecrets, ExchangeEnvironment],
            PrivateAccountClient,
        ] = create_account_client,
        paper_executor: PaperExecutionService | None = None,
    ) -> None:
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory
        self.paper_executor = paper_executor or PaperExecutionService(database)

    async def run_once(self) -> ReconciliationResult:
        await self.paper_executor.run_once()
        await self.database.set_execution_control(
            state="reconciling",
            reason="startup account reconciliation is running",
        )
        summaries = await self.credentials.list()
        if not summaries:
            await self.database.set_execution_control(
                state="blocked",
                reason="no exchange accounts are configured",
            )
            return ReconciliationResult(
                accounts_checked=0,
                accounts_blocked=0,
                accounts_failed=0,
                execution_state="blocked",
            )

        blocked = 0
        failed = 0
        for summary in summaries:
            client: PrivateAccountClient | None = None
            try:
                secrets = await self.credentials.load(
                    summary.exchange,
                    summary.environment,
                )
                if secrets is None:
                    raise RuntimeError("credential disappeared during reconciliation")
                client = self.account_client_factory(
                    summary.exchange,
                    secrets,
                    summary.environment,
                )
                snapshot, trading_state = await asyncio.gather(
                    client.snapshot(),
                    client.trading_state(),
                )
                reasons = [
                    "fills and private event streams have not been reconciled yet"
                ]
                if not trading_state.complete:
                    reasons.append(
                        trading_state.incomplete_reason
                        or "remote trading state is incomplete"
                    )
                if trading_state.open_orders:
                    reasons.append("remote open orders require local intent matching")
                if trading_state.positions:
                    reasons.append("remote positions require local pair matching")
                if snapshot.position_mode == PositionMode.UNKNOWN:
                    reasons.append("position mode is unknown")
                if snapshot.trade_permission is not True:
                    reasons.append("two-leg trade permission is not confirmed")
                await self.database.record_account_reconciliation(
                    exchange=summary.exchange.value,
                    environment=summary.environment.value,
                    status="blocked",
                    reason="; ".join(reasons),
                    snapshot=snapshot,
                    trading_state=trading_state,
                )
                blocked += 1
            except Exception:
                # Credential material, signed URLs, and exchange response bodies
                # must never be persisted as reconciliation reasons.
                await self.database.record_account_reconciliation(
                    exchange=summary.exchange.value,
                    environment=summary.environment.value,
                    status="error",
                    reason="private account reconciliation failed",
                )
                failed += 1
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass

        await self.database.set_execution_control(
            state="blocked",
            reason=(
                "startup reconciliation is incomplete; "
                "order, fill, and position matching is required before execution"
            ),
        )
        return ReconciliationResult(
            accounts_checked=len(summaries),
            accounts_blocked=blocked,
            accounts_failed=failed,
            execution_state="blocked",
        )

    async def run_forever(self, *, interval_seconds: float = 60) -> None:
        async with self.database.executor_lock() as acquired:
            if not acquired:
                raise WorkerLockUnavailable(
                    "another Basis Hawk execution worker holds the account lock"
                )
            while True:
                await self.run_once()
                await asyncio.sleep(interval_seconds)

    async def run_once_exclusive(self) -> ReconciliationResult:
        async with self.database.executor_lock() as acquired:
            if not acquired:
                raise WorkerLockUnavailable(
                    "another Basis Hawk execution worker holds the account lock"
                )
            return await self.run_once()
