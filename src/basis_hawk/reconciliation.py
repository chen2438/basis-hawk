from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

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
from basis_hawk.storage import Database, OrderLegRow
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
        control = await self.database.execution_control()
        safety_pause_reason = (
            control.reason if control is not None and control.state == "paused" else None
        )
        if safety_pause_reason is None:
            await self.database.set_execution_control(
                state="reconciling",
                reason="startup account reconciliation is running",
            )
        summaries = await self.credentials.list()
        if not summaries:
            if safety_pause_reason is None:
                await self.database.set_execution_control(
                    state="blocked",
                    reason="no exchange accounts are configured",
                )
            return ReconciliationResult(
                accounts_checked=0,
                accounts_blocked=0,
                accounts_failed=0,
                execution_state="paused" if safety_pause_reason else "blocked",
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
                reasons = ["private event streams have not been connected yet"]
                order_reconciliation_complete = True
                recovered_order_count = 0
                fill_reconciliation_complete = True
                fill_count = 0
                local_legs = await self.database.order_legs_for_reconciliation(
                    exchange=summary.exchange.value,
                    environment=summary.environment.value,
                )
                for leg in local_legs:
                    exchange_order_id = leg.exchange_order_id
                    if leg.status in {
                        "submitted",
                        "acknowledged",
                        "partially_filled",
                        "unknown",
                    }:
                        recovering_order_id = exchange_order_id is None
                        lookup = await client.order_by_client_id(
                            market=leg.market,
                            symbol=leg.symbol,
                            client_order_id=leg.client_order_id,
                        )
                        if not lookup.complete:
                            order_reconciliation_complete = False
                            reasons.append(
                                lookup.incomplete_reason
                                or "remote order lookup is incomplete"
                            )
                        elif lookup.order is None:
                            order_reconciliation_complete = False
                            reasons.append(
                                "submitted order was not found by client order ID"
                                if recovering_order_id
                                else "linked order was not found by client order ID"
                            )
                        else:
                            exchange_order_id = (
                                await self.database.reconcile_remote_order(
                                    order_leg_id=leg.id,
                                    order=lookup.order,
                                )
                            )
                            recovered_order_count += int(recovering_order_id)
                    if exchange_order_id is None:
                        if leg.status not in {"created", "failed", "canceled"}:
                            fill_reconciliation_complete = False
                            reasons.append(
                                "remote fills require a recovered exchange order ID"
                            )
                        continue
                    batch = await client.fills_for_order(
                        market=leg.market,
                        symbol=leg.symbol,
                        exchange_order_id=exchange_order_id,
                        client_order_id=leg.client_order_id,
                        since=leg.created_at - timedelta(minutes=5),
                    )
                    await self.database.persist_remote_fills(
                        order_leg_id=leg.id,
                        fills=batch.fills,
                    )
                    fill_count += len(batch.fills)
                    if not batch.complete:
                        fill_reconciliation_complete = False
                        reasons.append(
                            batch.incomplete_reason
                            or "remote order fills are incomplete"
                        )
                if (
                    order_reconciliation_complete
                    and fill_reconciliation_complete
                ):
                    legs_by_intent: dict[str, list[OrderLegRow]] = {}
                    for leg in local_legs:
                        legs_by_intent.setdefault(
                            leg.trade_intent_id,
                            [],
                        ).append(leg)
                    recoverable = await self.database.recoverable_trade_intents()
                    for intent in recoverable:
                        intent_legs = legs_by_intent.get(intent.id, [])
                        if (
                            intent.exchange == summary.exchange.value
                            and intent.environment == summary.environment.value
                            and intent.action == "open"
                            and intent.status == "executing"
                            and len(intent_legs) == 2
                            and {leg.leg for leg in intent_legs}
                            == {"spot", "perp"}
                        ):
                            await self.database.settle_live_open(
                                intent_id=intent.id
                            )
                if not trading_state.complete:
                    reasons.append(
                        trading_state.incomplete_reason or "remote trading state is incomplete"
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
                    order_reconciliation_complete=order_reconciliation_complete,
                    recovered_order_count=recovered_order_count,
                    fill_reconciliation_complete=fill_reconciliation_complete,
                    fill_count=fill_count,
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

        final_control = await self.database.execution_control()
        final_pause_reason = (
            final_control.reason
            if final_control is not None and final_control.state == "paused"
            else safety_pause_reason
        )
        if final_pause_reason is None:
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
            execution_state="paused" if final_pause_reason else "blocked",
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
