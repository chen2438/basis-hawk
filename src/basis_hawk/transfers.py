from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from basis_hawk.credentials import ExchangeEnvironment
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
        self.per_request_limit_usdt = per_request_limit_usdt
        self.daily_limit_usdt = daily_limit_usdt

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
            per_request_limit=self.per_request_limit_usdt,
            daily_limit=self.daily_limit_usdt,
            actor=actor,
        )
