from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from basis_hawk.accounts import (
    AccountSnapshot,
    PrivateAccountClient,
    create_account_client,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.storage import Database, InternalTransferRow


class InternalTransferDirection(StrEnum):
    SPOT_TO_PERP = "spot_to_perp"
    PERP_TO_SPOT = "perp_to_spot"


class InternalTransferRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    environment: ExchangeEnvironment
    direction: InternalTransferDirection
    amount_usdt: Decimal = Field(gt=0, decimal_places=18)


class InternalTransferLedger:
    def __init__(
        self,
        database: Database,
        *,
        per_request_limit_usdt: Decimal,
        daily_limit_usdt: Decimal,
    ) -> None:
        self.database = database
        self.default_per_request_limit_usdt = per_request_limit_usdt
        self.default_daily_limit_usdt = daily_limit_usdt

    async def plan(
        self,
        request: InternalTransferRequest,
        *,
        idempotency_key: uuid.UUID,
        actor: str,
    ) -> tuple[InternalTransferRow, bool]:
        payload = request.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        transfer_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"basis-hawk:transfer:{idempotency_key}",
            )
        )
        return await self.database.plan_internal_transfer(
            transfer_id=transfer_id,
            idempotency_key=str(idempotency_key),
            request_fingerprint=fingerprint,
            exchange=request.exchange.value,
            environment=request.environment.value,
            direction=request.direction.value,
            amount=request.amount_usdt,
            default_per_request_limit=(
                self.default_per_request_limit_usdt
            ),
            default_daily_limit=self.default_daily_limit_usdt,
            actor=actor,
        )


class InternalTransferExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    transfer_id: str | None = None
    action: str = "idle"
    status: str | None = None


class InternalTransferExecutionService:
    _recoverable_without_ack = {Exchange.BITGET, Exchange.GATE}

    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        *,
        account_client_factory: Callable[
            [Exchange, ExchangeSecrets, ExchangeEnvironment],
            PrivateAccountClient,
        ] = create_account_client,
        confirmation_timeout_seconds: int = 900,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("transfer confirmation timeout must be positive")
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory
        self.confirmation_timeout = timedelta(
            seconds=confirmation_timeout_seconds,
        )

    async def run_once(self) -> InternalTransferExecutionResult:
        active = await self.database.next_internal_transfer(
            statuses={"submitted", "pending"},
        )
        if active is not None:
            return await self._confirm(active)
        planned = await self.database.next_internal_transfer(statuses={"planned"})
        if planned is None:
            return InternalTransferExecutionResult()
        return await self._submit(planned)

    async def _client(
        self,
        row: InternalTransferRow,
    ) -> tuple[Exchange, ExchangeEnvironment, PrivateAccountClient] | None:
        exchange = Exchange(row.exchange)
        environment = ExchangeEnvironment(row.environment)
        secrets = await self.credentials.load(exchange, environment)
        if secrets is None:
            await self.database.finalize_internal_transfer(
                transfer_id=row.id,
                status="manual_review",
                error_code="credential_missing",
            )
            return None
        return (
            exchange,
            environment,
            self.account_client_factory(exchange, secrets, environment),
        )

    async def _submit(
        self,
        row: InternalTransferRow,
    ) -> InternalTransferExecutionResult:
        client_info = await self._client(row)
        if client_info is None:
            return InternalTransferExecutionResult(
                transfer_id=row.id,
                action="blocked",
                status="manual_review",
            )
        exchange, _, client = client_info
        try:
            try:
                snapshot = await client.snapshot()
                trading_state = await client.trading_state()
            except Exception:
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="preflight_retry",
                    status="planned",
                )
            if snapshot.shared_balance:
                await self.database.finalize_internal_transfer(
                    transfer_id=row.id,
                    status="failed",
                    error_code="shared_balance_transfer_not_required",
                )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="rejected",
                    status="failed",
                )
            if not trading_state.complete or trading_state.open_orders:
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="preflight_retry",
                    status="planned",
                )
            source_balance, target_balance = _transfer_balances(row, snapshot)
            if source_balance < row.amount:
                await self.database.finalize_internal_transfer(
                    transfer_id=row.id,
                    status="failed",
                    error_code="insufficient_source_balance",
                )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="rejected",
                    status="failed",
                )
            prepared = await self.database.prepare_internal_transfer_submission(
                transfer_id=row.id,
                source_balance=source_balance,
                target_balance=target_balance,
            )
            if prepared is None:
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="lost_claim",
                    status=row.status,
                )
            try:
                submission = await client.submit_internal_transfer(
                    transfer_id=row.id,
                    direction=row.direction,
                    amount=row.amount,
                )
            except Exception:
                if exchange not in self._recoverable_without_ack:
                    await self.database.finalize_internal_transfer(
                        transfer_id=row.id,
                        status="manual_review",
                        error_code="submission_ack_uncertain",
                    )
                    status = "manual_review"
                else:
                    status = "submitted"
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="submission_uncertain",
                    status=status,
                )
            persisted = await self.database.record_internal_transfer_remote_id(
                transfer_id=row.id,
                exchange_transfer_id=submission.transfer_id,
            )
            return InternalTransferExecutionResult(
                transfer_id=row.id,
                action="submitted",
                status=persisted.status,
            )
        finally:
            await _close_quietly(client)

    async def _confirm(
        self,
        row: InternalTransferRow,
    ) -> InternalTransferExecutionResult:
        client_info = await self._client(row)
        if client_info is None:
            return InternalTransferExecutionResult(
                transfer_id=row.id,
                action="blocked",
                status="manual_review",
            )
        exchange, _, client = client_info
        try:
            remote_id = row.exchange_transfer_id or ""
            if not remote_id and exchange not in self._recoverable_without_ack:
                await self.database.finalize_internal_transfer(
                    transfer_id=row.id,
                    status="manual_review",
                    error_code="submission_ack_missing",
                )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="blocked",
                    status="manual_review",
                )
            try:
                remote = await client.internal_transfer_status(
                    transfer_id=remote_id,
                    client_transfer_id=row.id,
                    direction=row.direction,
                    amount=row.amount,
                    created_at=row.created_at,
                )
            except Exception:
                if self._confirmation_timed_out(row):
                    await self.database.finalize_internal_transfer(
                        transfer_id=row.id,
                        status="manual_review",
                        error_code="transfer_confirmation_timeout",
                    )
                    return InternalTransferExecutionResult(
                        transfer_id=row.id,
                        action="blocked",
                        status="manual_review",
                    )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="confirmation_retry",
                    status=row.status,
                )
            if remote.transfer_id and remote.transfer_id != remote_id:
                row = await self.database.record_internal_transfer_remote_id(
                    transfer_id=row.id,
                    exchange_transfer_id=remote.transfer_id,
                )
            if remote.status == "failed":
                await self.database.finalize_internal_transfer(
                    transfer_id=row.id,
                    status="failed",
                    error_code="exchange_transfer_failed",
                )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="remote_failed",
                    status="failed",
                )
            if remote.status != "completed":
                if self._confirmation_timed_out(row):
                    await self.database.finalize_internal_transfer(
                        transfer_id=row.id,
                        status="manual_review",
                        error_code="transfer_confirmation_timeout",
                    )
                    return InternalTransferExecutionResult(
                        transfer_id=row.id,
                        action="blocked",
                        status="manual_review",
                    )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="confirmation_retry",
                    status=row.status,
                )
            try:
                snapshot = await client.snapshot()
            except Exception:
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="arrival_retry",
                    status=row.status,
                )
            _, target_balance = _transfer_balances(row, snapshot)
            if (
                row.expected_target_balance is None
                or target_balance < row.expected_target_balance
            ):
                if self._confirmation_timed_out(row):
                    await self.database.finalize_internal_transfer(
                        transfer_id=row.id,
                        status="manual_review",
                        error_code="arrival_confirmation_timeout",
                    )
                    return InternalTransferExecutionResult(
                        transfer_id=row.id,
                        action="blocked",
                        status="manual_review",
                    )
                return InternalTransferExecutionResult(
                    transfer_id=row.id,
                    action="arrival_retry",
                    status=row.status,
                )
            await self.database.finalize_internal_transfer(
                transfer_id=row.id,
                status="completed",
            )
            return InternalTransferExecutionResult(
                transfer_id=row.id,
                action="completed",
                status="completed",
            )
        finally:
            await _close_quietly(client)

    def _confirmation_timed_out(self, row: InternalTransferRow) -> bool:
        submitted_at = row.submitted_at or row.updated_at
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - submitted_at >= self.confirmation_timeout


def _transfer_balances(
    row: InternalTransferRow,
    snapshot: AccountSnapshot,
) -> tuple[Decimal, Decimal]:
    spot = Decimal(str(snapshot.spot_usdt_available))
    perp = Decimal(str(snapshot.perp_usdt_available))
    if row.direction == InternalTransferDirection.SPOT_TO_PERP.value:
        return spot, perp
    if row.direction == InternalTransferDirection.PERP_TO_SPOT.value:
        return perp, spot
    raise ValueError("unsupported internal transfer direction")


async def _close_quietly(client: PrivateAccountClient) -> None:
    try:
        await client.close()
    except Exception:
        pass
