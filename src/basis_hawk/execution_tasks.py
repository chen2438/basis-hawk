from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import Field

from basis_hawk.accounts import (
    PositionMode,
    PrivateAccountClient,
    PrivateRequestError,
    UnsupportedEnvironmentError,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.multi_leg import (
    DecimalPayload,
    ExecutionEnvironment,
    ExecutionTaskSpec,
)
from basis_hawk.order_books import (
    OrderBookUnavailable,
    RestOrderBookProvider,
)
from basis_hawk.storage import (
    Database,
    ExecutionTaskLegRow,
    ExecutionTaskRow,
)

PREFLIGHT_TTL = timedelta(seconds=60)


class ExecutionTaskValidationError(ValueError):
    pass


class ExecutionTaskConflict(ValueError):
    pass


class ExecutionTaskLegView(DecimalPayload):
    id: str
    ordinal: int
    account_id: str | None
    exchange: str
    role: str
    market_type: str
    side: str
    base_asset: str
    quote_asset: str
    symbol: str
    target_quantity: Decimal
    resolved_base_quantity: Decimal | None
    signed_base_ratio: Decimal | None
    per_order_quantity: Decimal
    order_mode: str
    maximum_slippage: Decimal
    maker_book_level: int | None
    maker_maximum_chases: int | None
    maker_fallback_mode: str | None
    margin_mode: str | None
    leverage: int
    reduce_only: bool


class ExecutionTaskView(DecimalPayload):
    id: str
    name: str
    display_symbol: str
    environment: str
    base_asset: str
    quantity_mode: str
    source_opportunity_id: str | None
    create_strategy: bool
    hedge_trigger: str
    hedge_threshold: Decimal | None
    maximum_base_exposure: Decimal
    maximum_notional_exposure_usdt: Decimal
    maximum_retries: int
    status: str
    failure_code: str | None
    preflight: dict[str, object] | None
    preflight_expires_at: datetime | None
    created_by: str
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    legs: list[ExecutionTaskLegView]


class ExecutionRunView(DecimalPayload):
    id: str
    run_number: int
    status: str
    worker_id: str | None
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExecutionOrderView(DecimalPayload):
    id: str
    run_id: str
    task_leg_id: str
    parent_order_id: str | None
    attempt_number: int
    chase_number: int
    client_order_id: str
    exchange_order_id: str | None
    order_mode: str
    side: str
    reduce_only: bool
    purpose: str
    status: str
    quantity: Decimal
    base_multiplier: Decimal
    limit_price: Decimal | None
    filled_quantity: Decimal
    average_price: Decimal | None
    failure_code: str | None
    submitted_at: datetime | None
    terminal_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExecutionFillView(DecimalPayload):
    id: str
    execution_order_id: str
    exchange_trade_id: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_asset: str
    liquidity: str
    occurred_at: datetime


class ExecutionTaskActivityView(DecimalPayload):
    runs: list[ExecutionRunView]
    orders: list[ExecutionOrderView]
    fills: list[ExecutionFillView]


AccountClientFactory = Callable[
    [Exchange, ExchangeSecrets, ExchangeEnvironment],
    PrivateAccountClient,
]


class ExecutionTaskService:
    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        account_client_factory: AccountClientFactory,
        order_books: RestOrderBookProvider | None = None,
    ) -> None:
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory
        self.order_books = order_books or RestOrderBookProvider()

    async def create(
        self,
        *,
        spec: ExecutionTaskSpec,
        idempotency_key: uuid.UUID,
        actor: str,
    ) -> tuple[ExecutionTaskView, bool]:
        fingerprint = _fingerprint(spec)
        existing = await self.database.execution_task_by_idempotency_key(
            str(idempotency_key)
        )
        if existing is not None:
            row, legs = existing
            if row.request_fingerprint != fingerprint:
                raise ExecutionTaskConflict(
                    "idempotency key was already used for another execution task"
                )
            return _view(row, legs), False
        await self._validate_accounts(spec)
        now = datetime.now(UTC)
        task_id = str(uuid.uuid4())
        row, legs, created = await self.database.create_execution_task(
            task={
                "id": task_id,
                "idempotency_key": str(idempotency_key),
                "request_fingerprint": fingerprint,
                "name": spec.name.strip(),
                "display_symbol": spec.display_symbol.strip(),
                "environment": spec.environment.value,
                "base_asset": spec.base_asset.upper(),
                "quantity_mode": spec.quantity_mode.value,
                "source_opportunity_id": spec.source_opportunity_id,
                "create_strategy": spec.create_strategy,
                "hedge_trigger": spec.hedge_trigger.value,
                "hedge_threshold": spec.hedge_threshold,
                "maximum_base_exposure": spec.maximum_base_exposure,
                "maximum_notional_exposure_usdt": (spec.maximum_notional_exposure_usdt),
                "maximum_retries": spec.maximum_retries,
                "status": "draft",
                "failure_code": None,
                "preflight_payload": None,
                "preflight_expires_at": None,
                "created_by": actor,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            },
            legs=[
                {
                    "id": str(uuid.uuid4()),
                    "task_id": task_id,
                    "account_id": leg.account_id,
                    "exchange": leg.exchange.value,
                    "ordinal": ordinal,
                    "role": leg.role.value,
                    "market_type": leg.market_type.value,
                    "side": leg.side.value,
                    "base_asset": leg.base_asset.upper(),
                    "quote_asset": leg.quote_asset.upper(),
                    "symbol": leg.symbol,
                    "target_quantity": leg.target_quantity,
                    "resolved_base_quantity": None,
                    "signed_base_ratio": None,
                    "per_order_quantity": leg.per_order_quantity,
                    "order_mode": leg.order_mode.value,
                    "maximum_slippage": leg.maximum_slippage,
                    "maker_book_level": (
                        leg.maker_policy.book_level
                        if leg.maker_policy is not None
                        else None
                    ),
                    "maker_maximum_chases": (
                        leg.maker_policy.maximum_chases
                        if leg.maker_policy is not None
                        else None
                    ),
                    "maker_fallback_mode": (
                        leg.maker_policy.fallback_mode.value
                        if leg.maker_policy is not None
                        and leg.maker_policy.fallback_mode is not None
                        else None
                    ),
                    "margin_mode": (
                        leg.margin_mode.value if leg.margin_mode is not None else None
                    ),
                    "leverage": leg.leverage,
                    "reduce_only": leg.reduce_only,
                    "created_at": now,
                    "updated_at": now,
                }
                for ordinal, leg in enumerate(spec.legs)
            ],
        )
        return _view(row, legs), created

    async def get(self, task_id: str) -> ExecutionTaskView | None:
        value = await self.database.execution_task(task_id)
        return _view(*value) if value is not None else None

    async def list(self, *, limit: int = 100) -> list[ExecutionTaskView]:
        return [
            _view(row, legs)
            for row, legs in await self.database.execution_tasks(limit=limit)
        ]

    async def activity(self, task_id: str) -> ExecutionTaskActivityView:
        if await self.database.execution_task(task_id) is None:
            raise KeyError(task_id)
        runs, orders, fills = await self.database.execution_task_activity(task_id)
        return ExecutionTaskActivityView(
            runs=[
                ExecutionRunView(
                    id=item.id,
                    run_number=item.run_number,
                    status=item.status,
                    worker_id=item.worker_id,
                    failure_code=item.failure_code,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in runs
            ],
            orders=[
                ExecutionOrderView(
                    id=item.id,
                    run_id=item.run_id,
                    task_leg_id=item.task_leg_id,
                    parent_order_id=item.parent_order_id,
                    attempt_number=item.attempt_number,
                    chase_number=item.chase_number,
                    client_order_id=item.client_order_id,
                    exchange_order_id=item.exchange_order_id,
                    order_mode=item.order_mode,
                    side=item.side,
                    reduce_only=item.reduce_only,
                    purpose=item.purpose,
                    status=item.status,
                    quantity=item.quantity,
                    base_multiplier=item.base_multiplier,
                    limit_price=item.limit_price,
                    filled_quantity=item.filled_quantity,
                    average_price=item.average_price,
                    failure_code=item.failure_code,
                    submitted_at=item.submitted_at,
                    terminal_at=item.terminal_at,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in orders
            ],
            fills=[
                ExecutionFillView(
                    id=item.id,
                    execution_order_id=item.execution_order_id,
                    exchange_trade_id=item.exchange_trade_id,
                    quantity=item.quantity,
                    price=item.price,
                    fee_amount=item.fee_amount,
                    fee_asset=item.fee_asset,
                    liquidity=item.liquidity,
                    occurred_at=item.occurred_at,
                )
                for item in fills
            ],
        )

    async def preflight(
        self,
        *,
        task_id: str,
        actor: str,
    ) -> ExecutionTaskView:
        value = await self.database.execution_task(task_id)
        if value is None:
            raise KeyError(task_id)
        row, legs = value
        payload = await self._preflight_payload(row, legs)
        expires_at = datetime.now(UTC) + PREFLIGHT_TTL
        updated = await self.database.mark_execution_task_preflight_ready(
            task_id=task_id,
            expected_version=row.version,
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            expires_at=expires_at,
            actor=actor,
        )
        refreshed = await self.database.execution_task(updated.id)
        if refreshed is None:
            raise KeyError(task_id)
        return _view(*refreshed)

    async def start(
        self,
        *,
        task_id: str,
        expected_version: int,
        actor: str,
    ) -> ExecutionTaskView:
        try:
            updated = await self.database.queue_execution_task(
                task_id=task_id,
                expected_version=expected_version,
                actor=actor,
            )
        except ValueError as exc:
            raise ExecutionTaskConflict(str(exc)) from exc
        refreshed = await self.database.execution_task(updated.id)
        if refreshed is None:
            raise KeyError(task_id)
        return _view(*refreshed)

    async def cancel(
        self,
        *,
        task_id: str,
        expected_version: int,
        actor: str,
    ) -> ExecutionTaskView:
        try:
            updated = await self.database.cancel_execution_task(
                task_id=task_id,
                expected_version=expected_version,
                actor=actor,
            )
        except ValueError as exc:
            raise ExecutionTaskConflict(str(exc)) from exc
        refreshed = await self.database.execution_task(updated.id)
        if refreshed is None:
            raise KeyError(task_id)
        return _view(*refreshed)

    async def _validate_accounts(self, spec: ExecutionTaskSpec) -> None:
        for account_id in {
            leg.account_id for leg in spec.legs if leg.account_id is not None
        }:
            summary = await self.credentials.summary(account_id)
            if summary is None:
                raise ExecutionTaskValidationError(
                    f"account {account_id} is not configured"
                )
            if spec.environment != ExecutionEnvironment.PAPER and (
                summary.environment.value != spec.environment.value
            ):
                raise ExecutionTaskValidationError(
                    f"account {account_id} environment does not match the task"
                )
            if any(
                leg.account_id == account_id and leg.exchange != summary.exchange
                for leg in spec.legs
            ):
                raise ExecutionTaskValidationError(
                    f"account {account_id} exchange does not match its task leg"
                )

    async def _preflight_payload(
        self,
        row: ExecutionTaskRow,
        legs: list[ExecutionTaskLegRow],
    ) -> dict[str, object]:
        if row.environment == ExecutionEnvironment.PAPER.value:
            return {
                "checked_at": datetime.now(UTC).isoformat(),
                "paper": True,
                "accounts": [],
            }
        account_payloads: list[dict[str, object]] = []
        for account_id in dict.fromkeys(
            leg.account_id for leg in legs if leg.account_id is not None
        ):
            if account_id is None:
                continue
            summary = await self.credentials.summary(account_id)
            secrets = await self.credentials.load_by_id(account_id)
            if summary is None or secrets is None:
                raise ExecutionTaskValidationError(
                    f"account {account_id} is not configured"
                )
            if summary.environment.value != row.environment:
                raise ExecutionTaskValidationError(
                    f"account {account_id} environment does not match the task"
                )
            client: PrivateAccountClient | None = None
            try:
                client = self.account_client_factory(
                    summary.exchange,
                    secrets,
                    summary.environment,
                )
                snapshot = await client.snapshot()
                trading_state = await client.trading_state()
            except (
                PrivateRequestError,
                UnsupportedEnvironmentError,
                ValueError,
            ) as exc:
                raise ExecutionTaskValidationError(
                    f"{summary.exchange.value} account preflight failed"
                ) from exc
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
            if snapshot.trade_permission is False:
                raise ExecutionTaskValidationError(
                    f"{summary.exchange.value} account is not permitted to trade"
                )
            if not trading_state.complete:
                raise ExecutionTaskValidationError(
                    f"{summary.exchange.value} trading state is incomplete"
                )
            if (
                any(
                    leg.account_id == account_id and leg.market_type == "perpetual"
                    for leg in legs
                )
                and snapshot.position_mode == PositionMode.UNKNOWN
            ):
                raise ExecutionTaskValidationError(
                    f"{summary.exchange.value} perpetual position mode is unknown"
                )
            account_payloads.append(
                {
                    "account_id": account_id,
                    "exchange": summary.exchange.value,
                    "environment": summary.environment.value,
                    "snapshot_observed_at": snapshot.observed_at.isoformat(),
                    "position_mode": snapshot.position_mode.value,
                    "perp_margin_mode": snapshot.perp_margin_mode.value,
                    "trade_permission": snapshot.trade_permission,
                    "open_order_count": len(trading_state.open_orders),
                    "position_count": len(trading_state.positions),
                    "trading_state_observed_at": (
                        trading_state.observed_at.isoformat()
                    ),
                }
            )
        maker_books: list[dict[str, object]] = []
        for leg in legs:
            if leg.order_mode != "maker":
                continue
            level = leg.maker_book_level or 1
            try:
                book = await self.order_books.fetch(
                    exchange=Exchange(leg.exchange),
                    environment=ExchangeEnvironment(row.environment),
                    market="spot" if leg.market_type == "spot" else "perp",
                    symbol=leg.symbol,
                    level=level,
                )
                price = book.maker_price(side=leg.side, level=level)
            except (OrderBookUnavailable, ValueError) as exc:
                raise ExecutionTaskValidationError(
                    f"{leg.exchange} {leg.symbol} maker depth is unavailable"
                ) from exc
            maker_books.append(
                {
                    "task_leg_id": leg.id,
                    "exchange": leg.exchange,
                    "market": leg.market_type,
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "level": level,
                    "price": format(price, "f"),
                    "observed_at": book.observed_at.isoformat(),
                }
            )
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "paper": False,
            "accounts": account_payloads,
            "maker_books": maker_books,
        }


def _fingerprint(spec: ExecutionTaskSpec) -> str:
    payload = json.dumps(
        spec.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _view(
    row: ExecutionTaskRow,
    legs: list[ExecutionTaskLegRow],
) -> ExecutionTaskView:
    preflight: dict[str, object] | None = None
    if row.preflight_payload:
        try:
            parsed = json.loads(row.preflight_payload)
            if isinstance(parsed, dict):
                preflight = parsed
        except ValueError:
            preflight = None
    return ExecutionTaskView(
        id=row.id,
        name=row.name,
        display_symbol=row.display_symbol,
        environment=row.environment,
        base_asset=row.base_asset,
        quantity_mode=row.quantity_mode,
        source_opportunity_id=row.source_opportunity_id,
        create_strategy=row.create_strategy,
        hedge_trigger=row.hedge_trigger,
        hedge_threshold=row.hedge_threshold,
        maximum_base_exposure=row.maximum_base_exposure,
        maximum_notional_exposure_usdt=row.maximum_notional_exposure_usdt,
        maximum_retries=row.maximum_retries,
        status=row.status,
        failure_code=row.failure_code,
        preflight=preflight,
        preflight_expires_at=row.preflight_expires_at,
        created_by=row.created_by,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        legs=[
            ExecutionTaskLegView(
                id=leg.id,
                ordinal=leg.ordinal,
                account_id=leg.account_id,
                exchange=leg.exchange,
                role=leg.role,
                market_type=leg.market_type,
                side=leg.side,
                base_asset=leg.base_asset,
                quote_asset=leg.quote_asset,
                symbol=leg.symbol,
                target_quantity=leg.target_quantity,
                resolved_base_quantity=leg.resolved_base_quantity,
                signed_base_ratio=leg.signed_base_ratio,
                per_order_quantity=leg.per_order_quantity,
                order_mode=leg.order_mode,
                maximum_slippage=leg.maximum_slippage,
                maker_book_level=leg.maker_book_level,
                maker_maximum_chases=leg.maker_maximum_chases,
                maker_fallback_mode=leg.maker_fallback_mode,
                margin_mode=leg.margin_mode,
                leverage=leg.leverage,
                reduce_only=leg.reduce_only,
            )
            for leg in legs
        ],
    )
