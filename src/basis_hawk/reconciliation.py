from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import (
    PositionMode,
    PrivateAccountClient,
    RemoteOrder,
    RemotePosition,
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
                private_stream_ready = await self.database.private_stream_ready(
                    exchange=summary.exchange.value,
                    environment=summary.environment.value,
                )
                reasons = []
                if not private_stream_ready:
                    reasons.append(
                        "private event stream is disconnected, incomplete, or stale"
                    )
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
                reasons.extend(
                    _open_order_reasons(
                        trading_state.open_orders,
                        local_legs,
                    )
                )
                expected_positions = await self.database.paired_perp_exposures(
                    exchange=summary.exchange.value,
                    environment=summary.environment.value,
                )
                reasons.extend(
                    _position_reasons(
                        trading_state.positions,
                        expected_positions,
                    )
                )
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
                    private_stream_ready=private_stream_ready,
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


def _open_order_reasons(
    remote_orders: list[RemoteOrder],
    local_legs: list[OrderLegRow],
) -> list[str]:
    by_exchange_id = {
        item.exchange_order_id: item
        for item in local_legs
        if item.exchange_order_id is not None
    }
    by_client_id = {item.client_order_id: item for item in local_legs}
    active = {"submitted", "acknowledged", "partially_filled", "unknown"}
    reasons: list[str] = []
    matched = 0
    for order in remote_orders:
        exchange_match = by_exchange_id.get(order.exchange_order_id)
        client_match = (
            by_client_id.get(order.client_order_id)
            if order.client_order_id is not None
            else None
        )
        if (
            exchange_match is not None
            and client_match is not None
            and exchange_match.id != client_match.id
        ):
            reasons.append("remote open order identifiers match different local legs")
            continue
        leg = exchange_match or client_match
        if leg is None:
            reasons.append("remote open order has no matching local intent")
            continue
        if (
            leg.status not in active
            or order.market != leg.market
            or order.symbol != leg.symbol
            or order.side != leg.side
            or not _decimal_equal(order.original_quantity, leg.quantity)
            or order.reduce_only != leg.reduce_only
        ):
            reasons.append("remote open order conflicts with its local order leg")
            continue
        matched += 1
    if matched:
        reasons.append("locally linked IOC orders are still open")
    return reasons


def _position_reasons(
    remote_positions: list[RemotePosition],
    expected_positions: list[tuple[str, Decimal, int]],
) -> list[str]:
    expected: dict[str, tuple[Decimal, int]] = {}
    reasons: list[str] = []
    for symbol, quantity, leverage in expected_positions:
        previous = expected.get(symbol)
        if previous is not None and previous[1] != leverage:
            reasons.append("local paired positions use conflicting leverage")
            continue
        expected[symbol] = (
            (previous[0] if previous is not None else Decimal("0")) + quantity,
            leverage,
        )
    remote: dict[str, tuple[Decimal, Decimal, bool | None]] = {}
    for item in remote_positions:
        if item.side != "short":
            reasons.append("remote position is not a strategy short position")
            continue
        previous = remote.get(item.symbol)
        if previous is not None and previous[1] != item.leverage:
            reasons.append("remote short positions use conflicting leverage")
            continue
        remote[item.symbol] = (
            (previous[0] if previous is not None else Decimal("0"))
            + item.quantity,
            item.leverage,
            item.isolated if previous is None else previous[2] and item.isolated,
        )
    for symbol, (quantity, leverage) in expected.items():
        actual = remote.pop(symbol, None)
        if actual is None:
            reasons.append("local paired position is missing from the exchange")
            continue
        if (
            not _decimal_equal(actual[0], quantity)
            or actual[1] != Decimal(leverage)
            or actual[2] is not True
        ):
            reasons.append("remote short position conflicts with the local pair")
    if remote:
        reasons.append("remote position has no matching local pair")
    return reasons


def _decimal_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.000000000000001")
