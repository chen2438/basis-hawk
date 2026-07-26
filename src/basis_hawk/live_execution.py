from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import (
    AccountSnapshot,
    LimitIocOrder,
    PositionMode,
    PrivateAccountClient,
    RemotePosition,
    create_account_client,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.models import Exchange
from basis_hawk.storage import Database, OrderLegRow, TradeIntentRow


class LiveExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    submitted: int
    uncertain: int
    preflight_failed: int


class LiveExecutionService:
    def __init__(
        self,
        database: Database,
        credentials: CredentialService,
        *,
        account_client_factory: Callable[
            [Exchange, ExchangeSecrets, ExchangeEnvironment],
            PrivateAccountClient,
        ] = create_account_client,
    ) -> None:
        self.database = database
        self.credentials = credentials
        self.account_client_factory = account_client_factory

    async def run_once(self) -> LiveExecutionResult:
        control = await self.database.execution_control()
        if control is None or control.state not in {"ready", "paused"}:
            return LiveExecutionResult(
                examined=0,
                submitted=0,
                uncertain=0,
                preflight_failed=0,
            )
        recoverable = await self.database.recoverable_trade_intents()
        candidates = [
            item
            for item in recoverable
            if item.environment in {"sandbox", "live"}
            and item.action in {"open", "close"}
            and item.status == "planned"
            and (
                control.state == "ready"
                or (item.action == "close" and item.emergency)
            )
        ]
        submitted = 0
        uncertain = 0
        preflight_failed = 0
        examined = 0
        for item in candidates:
            examined += 1
            now = datetime.now(UTC)
            market_observed_at = _utc(item.market_observed_at)
            if (
                market_observed_at > now + timedelta(seconds=5)
                or now - market_observed_at > timedelta(seconds=15)
            ):
                await self.database.expire_planned_trade_intent(
                    intent_id=item.id
                )
                continue
            try:
                did_submit, uncertain_legs = await self._execute(item)
            except Exception:
                preflight_failed += 1
                await self.database.set_execution_control(
                    state="paused",
                    reason=(
                        "live order preflight failed; account reconciliation "
                        "and operator review are required"
                    ),
                )
                break
            submitted += int(did_submit)
            uncertain += uncertain_legs
            if did_submit or uncertain_legs:
                break
        return LiveExecutionResult(
            examined=examined,
            submitted=submitted,
            uncertain=uncertain,
            preflight_failed=preflight_failed,
        )

    async def _execute(self, intent: TradeIntentRow) -> tuple[bool, int]:
        exchange = Exchange(intent.exchange)
        environment = ExchangeEnvironment(intent.environment)
        secrets = await self.credentials.load(exchange, environment)
        if secrets is None:
            raise RuntimeError("exchange credential is not configured")
        client = self.account_client_factory(exchange, secrets, environment)
        try:
            snapshot, remote_state = await asyncio.gather(
                client.snapshot(),
                client.trading_state(),
            )
            if (
                snapshot.exchange != exchange
                or snapshot.environment != environment
                or snapshot.observed_at < datetime.now(UTC) - timedelta(seconds=30)
            ):
                raise RuntimeError("private account snapshot is not current")
            if snapshot.trade_permission is not True:
                raise RuntimeError("two-leg trade permission is not confirmed")
            if snapshot.position_mode == PositionMode.UNKNOWN:
                raise RuntimeError("perpetual position mode is unknown")
            if not remote_state.complete:
                raise RuntimeError("remote account state is incomplete")
            if remote_state.open_orders:
                raise RuntimeError("remote account has open orders")
            current = await self.database.trade_intent(intent.id)
            if current is None:
                raise RuntimeError("trade intent disappeared before submission")
            _, legs = current
            primary = {item.leg: item for item in legs if item.leg in {"spot", "perp"}}
            if set(primary) != {"spot", "perp"}:
                raise RuntimeError("trade intent does not contain two primary legs")
            if intent.action == "open":
                if remote_state.positions:
                    raise RuntimeError("remote account has an existing position")
                self._validate_balance(snapshot, intent, primary)
                await client.configure_perp(
                    symbol=primary["perp"].symbol,
                    leverage=intent.leverage,
                    position_mode=snapshot.position_mode,
                )
            else:
                await self._validate_close_state(
                    intent,
                    primary,
                    remote_state.positions,
                )
            prepared = await self.database.prepare_live_submission(
                intent_id=intent.id
            )
            if prepared is None or not prepared[2]:
                return False, 0
            prepared_legs = {
                item.leg: item for item in prepared[1] if item.leg in {"spot", "perp"}
            }
            ordered_legs = [prepared_legs["spot"], prepared_legs["perp"]]
            results = await asyncio.gather(
                *(
                    client.place_limit_ioc(
                        LimitIocOrder(
                            market=leg.market,
                            symbol=leg.symbol,
                            side=leg.side,
                            quantity=leg.quantity,
                            limit_price=leg.limit_price,
                            client_order_id=leg.client_order_id,
                            reduce_only=leg.reduce_only,
                            position_mode=(
                                snapshot.position_mode
                                if leg.market == "perp"
                                else PositionMode.UNKNOWN
                            ),
                        )
                    )
                    for leg in ordered_legs
                ),
                return_exceptions=True,
            )
            uncertain = 0
            for leg, result in zip(ordered_legs, results, strict=True):
                if isinstance(result, BaseException):
                    await self.database.mark_order_submission_unknown(
                        order_leg_id=leg.id
                    )
                    uncertain += 1
                    continue
                try:
                    await self.database.record_order_submission(
                        order_leg_id=leg.id,
                        submission=result,
                    )
                except Exception:
                    await self.database.mark_order_submission_unknown(
                        order_leg_id=leg.id
                    )
                    uncertain += 1
            return True, uncertain
        finally:
            await client.close()

    @staticmethod
    def _validate_balance(
        snapshot: AccountSnapshot,
        intent: TradeIntentRow,
        primary: dict[str, OrderLegRow],
    ) -> None:
        spot_required = primary["spot"].quantity * primary["spot"].limit_price
        perp_base_quantity = (
            primary["perp"].quantity * primary["perp"].base_multiplier
        )
        perp_required = (
            perp_base_quantity
            * primary["perp"].limit_price
            / Decimal(intent.leverage)
        )
        if snapshot.shared_balance:
            if snapshot.spot_usdt_available < spot_required + perp_required:
                raise RuntimeError("shared USDT balance is insufficient")
        elif (
            snapshot.spot_usdt_available < spot_required
            or snapshot.perp_usdt_available < perp_required
        ):
            raise RuntimeError("spot or perpetual USDT balance is insufficient")

    async def _validate_close_state(
        self,
        intent: TradeIntentRow,
        primary: dict[str, OrderLegRow],
        remote_positions: list[RemotePosition],
    ) -> None:
        if intent.paired_position_id is None:
            raise RuntimeError("live close intent has no paired position")
        position = await self.database.paired_position(
            intent.paired_position_id
        )
        if (
            position is None
            or position.status != "closing"
            or position.closing_intent_id != intent.id
        ):
            raise RuntimeError("paired position is not reserved for closing")
        spot_base = (
            primary["spot"].quantity * primary["spot"].base_multiplier
        )
        perp_base = (
            primary["perp"].quantity * primary["perp"].base_multiplier
        )
        if (
            not _decimal_equal(spot_base, position.quantity)
            or not _decimal_equal(perp_base, position.quantity)
            or primary["spot"].side != "sell"
            or primary["spot"].reduce_only
            or primary["perp"].side != "buy"
            or not primary["perp"].reduce_only
        ):
            raise RuntimeError(
                "live close legs do not exactly reduce the paired position"
            )
        expected_rows = await self.database.paired_perp_exposures(
            exchange=intent.exchange,
            environment=intent.environment,
        )
        expected: dict[str, tuple[Decimal, int]] = {}
        for symbol, quantity, leverage in expected_rows:
            previous = expected.get(symbol)
            if previous is not None and previous[1] != leverage:
                raise RuntimeError(
                    "local paired positions use conflicting leverage"
                )
            expected[symbol] = (
                (previous[0] if previous is not None else Decimal("0"))
                + quantity,
                leverage,
            )
        remote: dict[str, tuple[Decimal, Decimal, bool | None]] = {}
        for item in remote_positions:
            if item.side != "short":
                raise RuntimeError(
                    "remote position is not a strategy short position"
                )
            previous = remote.get(item.symbol)
            if previous is not None and previous[1] != item.leverage:
                raise RuntimeError(
                    "remote short positions use conflicting leverage"
                )
            remote[item.symbol] = (
                (previous[0] if previous is not None else Decimal("0"))
                + item.quantity,
                item.leverage,
                (
                    item.isolated
                    if previous is None
                    else previous[2] is True and item.isolated is True
                ),
            )
        for symbol, (quantity, leverage) in expected.items():
            actual = remote.pop(symbol, None)
            if (
                actual is None
                or not _decimal_equal(actual[0], quantity)
                or actual[1] != Decimal(leverage)
                or actual[2] is not True
            ):
                raise RuntimeError(
                    "remote short position conflicts with the local pair"
                )
        if remote:
            raise RuntimeError("remote position has no matching local pair")


def _decimal_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.000000000000001")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
