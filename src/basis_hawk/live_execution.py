from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from basis_hawk.accounts import (
    AccountSnapshot,
    LimitIocOrder,
    PerpMarginMode,
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
from basis_hawk.trading import protective_limit_price


class LiveExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    submitted: int
    uncertain: int
    preflight_failed: int


class LiveCompensationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    examined: int
    submitted: int
    uncertain: int
    failed: int


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
            snapshot = await client.snapshot()
            remote_state = await client.trading_state()
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
                LiveCompensationService._validate_balance(
                    snapshot,
                    intent,
                    primary,
                )
                await client.configure_perp(
                    symbol=primary["perp"].symbol,
                    leverage=intent.leverage,
                    position_mode=snapshot.position_mode,
                )
            else:
                await LiveCompensationService._validate_close_state(
                    self,
                    intent,
                    primary,
                    remote_state.positions,
                    expected_isolated=(
                        snapshot.perp_margin_mode == PerpMarginMode.ISOLATED
                    ),
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


class LiveCompensationService:
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

    async def run_once(self) -> LiveCompensationResult:
        control = await self.database.execution_control()
        if control is None or control.state != "paused":
            return LiveCompensationResult(
                examined=0,
                submitted=0,
                uncertain=0,
                failed=0,
            )
        candidates = [
            item
            for item in await self.database.recoverable_trade_intents()
            if item.environment in {"sandbox", "live"}
            and item.status == "compensating"
        ]
        if not candidates:
            return LiveCompensationResult(
                examined=0,
                submitted=0,
                uncertain=0,
                failed=0,
            )
        intent = candidates[0]
        try:
            submitted, uncertain = await self._execute(intent)
        except Exception:
            return LiveCompensationResult(
                examined=1,
                submitted=0,
                uncertain=0,
                failed=1,
            )
        return LiveCompensationResult(
            examined=1,
            submitted=int(submitted),
            uncertain=uncertain,
            failed=0,
        )

    async def _execute(
        self,
        intent: TradeIntentRow,
    ) -> tuple[bool, int]:
        current = await self.database.trade_intent(intent.id)
        if current is None:
            raise RuntimeError("compensating intent disappeared")
        compensations = [
            item
            for item in current[1]
            if item.leg.endswith("_compensation")
        ]
        if len(compensations) != 1:
            raise RuntimeError(
                "compensating intent has no unique protection leg"
            )
        compensation = compensations[0]
        if compensation.status != "created":
            return False, 0
        exchange = Exchange(intent.exchange)
        environment = ExchangeEnvironment(intent.environment)
        opportunities = await self.database.latest_opportunities(
            exchanges={intent.exchange}
        )
        opportunity = next(
            (
                item
                for item in opportunities
                if item.base_asset == intent.base_asset
            ),
            None,
        )
        pairs = await self.database.instrument_pairs(
            exchanges={intent.exchange}
        )
        pair = next(
            (item for item in pairs if item.base_asset == intent.base_asset),
            None,
        )
        now = datetime.now(UTC)
        if (
            opportunity is None
            or pair is None
            or not pair.trading_rules_complete
            or _utc(opportunity.observed_at)
            < now - timedelta(seconds=15)
        ):
            raise RuntimeError(
                "fresh compensation market and rules are unavailable"
            )
        reference_price = {
            ("spot", "buy"): opportunity.spot_ask,
            ("spot", "sell"): opportunity.spot_bid,
            ("perp", "buy"): opportunity.perp_ask,
            ("perp", "sell"): opportunity.perp_bid,
        }[(compensation.market, compensation.side)]
        price_increment = (
            pair.spot_price_increment
            if compensation.market == "spot"
            else pair.perp_price_increment
        )
        maximum_slippage = await self._maximum_slippage(
            environment=intent.environment
        )
        limit_price = protective_limit_price(
            reference_price=reference_price,
            maximum_slippage=maximum_slippage,
            side=compensation.side,
            price_increment=price_increment,
        )
        base_quantity = (
            compensation.quantity * compensation.base_multiplier
        )
        capacity = (
            opportunity.close_top_book_notional
            if (
                (compensation.market == "spot"
                 and compensation.side == "sell")
                or (
                    compensation.market == "perp"
                    and compensation.side == "buy"
                )
            )
            else opportunity.top_book_notional
        )
        if base_quantity * reference_price > capacity:
            raise RuntimeError(
                "compensation exceeds the current protected top book"
            )
        secrets = await self.credentials.load(exchange, environment)
        if secrets is None:
            raise RuntimeError("exchange credential is not configured")
        client = self.account_client_factory(
            exchange,
            secrets,
            environment,
        )
        try:
            snapshot = await client.snapshot()
            remote_state = await client.trading_state()
            if (
                snapshot.trade_permission is not True
                or snapshot.position_mode == PositionMode.UNKNOWN
                or not remote_state.complete
                or remote_state.open_orders
            ):
                raise RuntimeError(
                    "account is not safe for compensation submission"
                )
            prepared = await self.database.prepare_live_compensation(
                intent_id=intent.id,
                limit_price=limit_price,
            )
            if prepared is None or not prepared[2]:
                return False, 0
            leg = prepared[1]
            try:
                submission = await client.place_limit_ioc(
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
                await self.database.record_order_submission(
                    order_leg_id=leg.id,
                    submission=submission,
                )
            except Exception:
                await self.database.mark_order_submission_unknown(
                    order_leg_id=leg.id
                )
                return True, 1
            return True, 0
        finally:
            await client.close()

    async def _maximum_slippage(self, *, environment: str) -> Decimal:
        control = await self.database.automation_control()
        if control.active_strategy_id is None:
            return Decimal("0.01")
        strategy = await self.database.strategy_version(
            control.active_strategy_id
        )
        if strategy is None or strategy.environment != environment:
            return Decimal("0.01")
        value = json.loads(strategy.payload).get(
            "emergency_max_slippage",
            "0.01",
        )
        maximum = Decimal(str(value))
        if maximum <= 0 or maximum > Decimal("0.25"):
            raise RuntimeError(
                "configured emergency compensation slippage is invalid"
            )
        return maximum

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
            if (
                snapshot.spot_usdt_available < spot_required
                or snapshot.perp_usdt_available < spot_required + perp_required
            ):
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
        *,
        expected_isolated: bool,
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
                or actual[2] is not expected_isolated
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
