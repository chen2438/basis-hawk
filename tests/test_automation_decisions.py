from datetime import UTC, datetime, timedelta
from decimal import Decimal

from basis_hawk.automation import (
    GATE_AUTOMATIC_DEPTH_CANDIDATES,
    AutomaticTradingService,
    AutomationPosition,
    AutoStrategyConfig,
    evaluate_automatic_strategy,
)
from basis_hawk.credentials import ExchangeEnvironment
from basis_hawk.models import (
    Exchange,
    FundingObservation,
    InstrumentPair,
    MarketQuote,
    Opportunity,
    Quality,
)
from basis_hawk.storage import Database


def _config(**changes: object) -> AutoStrategyConfig:
    values: dict[str, object] = {
        "environment": "live",
        "enabled_exchanges": {Exchange.BINANCE, Exchange.OKX},
        "leverage": 2,
        "notional_per_trade": Decimal("100"),
        "per_exchange_max_exposure": Decimal("200"),
        "global_max_exposure": Decimal("300"),
        "max_concurrent_positions": 3,
        "minimum_current_apr": Decimal("0.10"),
        "minimum_apr_24h": Decimal("0.08"),
        "minimum_apr_7d": Decimal("0.06"),
        "minimum_net_return": Decimal("0.005"),
        "minimum_opening_basis": Decimal("0"),
        "maximum_opening_basis": Decimal("0.03"),
        "minimum_two_leg_notional": Decimal("50"),
        "book_capacity_multiple": Decimal("2"),
        "normal_max_slippage": Decimal("0.001"),
        "emergency_max_slippage": Decimal("0.01"),
        "daily_max_loss": Decimal("50"),
        "minimum_reentry_minutes": 60,
        "maximum_holding_hours": 72,
        "minimum_liquidation_buffer": Decimal("0.20"),
        "close_funding_rate_below": Decimal("0"),
        "close_net_return_below": Decimal("0"),
        "close_basis_above": Decimal("0.04"),
        "take_profit_usdt": Decimal("10"),
        "stop_loss_usdt": Decimal("10"),
    }
    values.update(changes)
    return AutoStrategyConfig.model_validate(values)


def _opportunity(
    *,
    exchange: Exchange = Exchange.BINANCE,
    base_asset: str = "ORDER",
    observed_at: datetime,
    net_return: Decimal | None = Decimal("0.01"),
    current_apr: Decimal = Decimal("0.20"),
    quality: Quality = Quality.HEALTHY,
) -> Opportunity:
    return Opportunity(
        exchange=exchange,
        base_asset=base_asset,
        spot_symbol=f"{base_asset}USDT",
        perp_symbol=f"{base_asset}USDT",
        observed_at=observed_at,
        spot_bid=Decimal("9.99"),
        spot_ask=Decimal("10"),
        perp_bid=Decimal("10.10"),
        perp_ask=Decimal("10.11"),
        executable_basis=Decimal("0.01"),
        top_book_notional=Decimal("1000"),
        close_top_book_notional=Decimal("1000"),
        current_funding_rate=Decimal("0.0001"),
        funding_interval_hours=Decimal("8"),
        next_funding_at=None,
        current_apr=current_apr,
        apr_24h=Decimal("0.15"),
        apr_7d=Decimal("0.12"),
        net_return=net_return,
        spot_quote_volume_24h=Decimal("2000000"),
        perp_quote_volume_24h=Decimal("3000000"),
        spot_taker_fee=Decimal("0.001"),
        perp_taker_fee=Decimal("0.0005"),
        quality=quality,
    )


def _position(
    *,
    now: datetime,
    exchange: Exchange = Exchange.BINANCE,
    base_asset: str = "OPEN",
    status: str = "open",
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
    liquidation_buffer: Decimal | None = Decimal("0.50"),
) -> AutomationPosition:
    return AutomationPosition(
        id=f"{exchange.value}-{base_asset}",
        exchange=exchange,
        environment="live",
        base_asset=base_asset,
        quantity=Decimal("10"),
        spot_entry_price=Decimal("10"),
        perp_entry_price=Decimal("10.10"),
        remaining_opening_fees_usdt=Decimal("0.15"),
        status=status,
        opened_at=opened_at or now - timedelta(hours=1),
        closed_at=closed_at,
        liquidation_buffer=liquidation_buffer,
    )


def test_opening_selects_highest_net_return_that_passes_all_rules() -> None:
    now = datetime.now(UTC)
    lower = _opportunity(
        base_asset="LOW",
        observed_at=now,
        net_return=Decimal("0.01"),
    )
    higher = _opportunity(
        exchange=Exchange.OKX,
        base_asset="HIGH",
        observed_at=now,
        net_return=Decimal("0.02"),
    )

    evaluation = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[lower, higher],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert evaluation.decision is not None
    assert evaluation.decision.action == "open"
    assert evaluation.decision.opportunity.key == "okx:HIGH"
    assert evaluation.decision.notional_usdt == Decimal("100")


def test_opening_basis_must_stay_between_configured_bounds() -> None:
    now = datetime.now(UTC)
    below = _opportunity(
        base_asset="BELOW",
        observed_at=now,
    ).model_copy(update={"executable_basis": Decimal("-0.001")})
    above = _opportunity(
        base_asset="ABOVE",
        observed_at=now,
    ).model_copy(update={"executable_basis": Decimal("0.031")})

    below_result = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[below],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )
    above_result = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[above],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert below_result.decision is None
    assert above_result.decision is None


def test_legacy_strategy_without_minimum_basis_keeps_its_original_behavior() -> None:
    values = _config().model_dump()
    values.pop("minimum_opening_basis")

    restored = AutoStrategyConfig.model_validate(values)

    assert restored.minimum_opening_basis == Decimal("-0.999999999999")


def test_opening_notional_shrinks_to_safe_book_capacity() -> None:
    now = datetime.now(UTC)
    opportunity = _opportunity(observed_at=now).model_copy(
        update={"top_book_notional": Decimal("150")}
    )

    evaluation = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[opportunity],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert evaluation.decision is not None
    assert evaluation.decision.notional_usdt == Decimal("75")


def test_opening_skips_capacity_below_minimum_notional() -> None:
    now = datetime.now(UTC)
    opportunity = _opportunity(observed_at=now).model_copy(
        update={"top_book_notional": Decimal("99")}
    )

    evaluation = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[opportunity],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert evaluation.decision is None


def test_opening_notional_shrinks_to_remaining_exposure() -> None:
    now = datetime.now(UTC)
    opportunity = _opportunity(observed_at=now)

    evaluation = evaluate_automatic_strategy(
        config=_config(
            per_exchange_max_exposure=Decimal("150"),
            minimum_two_leg_notional=Decimal("25"),
        ),
        opportunities=[opportunity],
        positions=[_position(now=now)],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert evaluation.decision is not None
    assert evaluation.decision.notional_usdt == Decimal("50")


def test_opening_is_blocked_by_daily_loss_and_concurrency() -> None:
    now = datetime.now(UTC)
    opportunity = _opportunity(observed_at=now)

    loss_block = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[opportunity],
        positions=[],
        daily_realized_pnl=Decimal("-50"),
        now=now,
    )
    concurrency_block = evaluate_automatic_strategy(
        config=_config(max_concurrent_positions=1),
        opportunities=[opportunity],
        positions=[_position(now=now)],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert loss_block.decision is None
    assert loss_block.opening_block_reason == (
        "daily realized loss limit was reached"
    )
    assert concurrency_block.decision is None
    assert concurrency_block.opening_block_reason == (
        "maximum concurrent positions was reached"
    )


def test_opening_respects_reentry_exposure_and_quote_freshness() -> None:
    now = datetime.now(UTC)
    opportunity = _opportunity(observed_at=now)
    recently_closed = _position(
        now=now,
        base_asset="ORDER",
        status="closed",
        closed_at=now - timedelta(minutes=30),
    )
    stale = opportunity.model_copy(
        update={"observed_at": now - timedelta(seconds=16)}
    )

    reentry = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[opportunity],
        positions=[recently_closed],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )
    exposure = evaluate_automatic_strategy(
        config=_config(per_exchange_max_exposure=Decimal("100")),
        opportunities=[opportunity],
        positions=[_position(now=now)],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )
    stale_result = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[stale],
        positions=[],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert reentry.decision is None
    assert exposure.decision is None
    assert stale_result.decision is None


def test_close_is_prioritized_over_daily_loss_opening_block() -> None:
    now = datetime.now(UTC)
    position = _position(
        now=now,
        opened_at=now - timedelta(hours=73),
    )
    opportunity = _opportunity(
        base_asset=position.base_asset,
        observed_at=now,
    )

    evaluation = evaluate_automatic_strategy(
        config=_config(),
        opportunities=[opportunity],
        positions=[position],
        daily_realized_pnl=Decimal("-100"),
        now=now,
    )

    assert evaluation.decision is not None
    assert evaluation.decision.action == "close"
    assert evaluation.decision.position_id == position.id
    assert evaluation.decision.reason == "maximum holding period was reached"


def test_low_liquidation_buffer_has_highest_close_priority() -> None:
    now = datetime.now(UTC)
    stop_position = _position(
        now=now,
        base_asset="STOP",
        opened_at=now - timedelta(hours=80),
    )
    liquidation_position = _position(
        now=now,
        exchange=Exchange.OKX,
        base_asset="LIQ",
        liquidation_buffer=Decimal("0.10"),
    )
    opportunities = [
        _opportunity(
            base_asset=stop_position.base_asset,
            observed_at=now,
        ),
        _opportunity(
            exchange=Exchange.OKX,
            base_asset=liquidation_position.base_asset,
            observed_at=now,
        ),
    ]

    evaluation = evaluate_automatic_strategy(
        config=_config(),
        opportunities=opportunities,
        positions=[stop_position, liquidation_position],
        daily_realized_pnl=Decimal("0"),
        now=now,
    )

    assert evaluation.decision is not None
    assert evaluation.decision.position_id == liquidation_position.id
    assert evaluation.decision.reason == (
        "liquidation buffer is below the configured minimum"
    )


async def test_automatic_service_plans_once_across_worker_restart() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime.now(UTC)
    config = _config(enabled_exchanges={Exchange.BINANCE})
    strategy = await database.create_strategy_version(
        environment="live",
        payload=config.model_dump(mode="json"),
        actor="test",
    )
    await database.set_automation_control(
        state="enabled",
        active_strategy_id=strategy.id,
        reason="test",
        actor="test",
    )
    await database.set_execution_control(state="ready", reason="test")
    await database.save_latest_opportunities(
        [
            _opportunity(observed_at=now).model_copy(
                update={"top_book_notional": Decimal("150")}
            )
        ]
    )
    await database.replace_instruments(
        "binance",
        [
            InstrumentPair(
                exchange=Exchange.BINANCE,
                base_asset="ORDER",
                spot_symbol="ORDERUSDT",
                perp_symbol="ORDERUSDT",
                spot_price_increment=Decimal("0.01"),
                spot_quantity_increment=Decimal("0.01"),
                spot_min_quantity=Decimal("0.01"),
                spot_min_notional=Decimal("5"),
                perp_price_increment=Decimal("0.01"),
                perp_quantity_increment=Decimal("0.01"),
                perp_min_quantity=Decimal("0.01"),
                perp_min_notional=Decimal("5"),
                perp_contract_size=Decimal("1"),
            )
        ],
    )

    first = await AutomaticTradingService(database).run_once()
    repeated = await AutomaticTradingService(database).run_once()

    assert first.created is True
    assert first.action == "open"
    assert first.intent_id is not None
    assert repeated.created is False
    assert await database.active_open_intent_keys(
        environment="live"
    ) == {"binance:ORDER"}
    recoverable = await database.recoverable_trade_intents()
    assert [item.id for item in recoverable] == [first.intent_id]
    assert recoverable[0].requested_notional == Decimal("75")
    await database.close()


class _GateDepthAdapter:
    def __init__(self, pairs: list[InstrumentPair]) -> None:
        self.pairs = pairs
        self.calls: list[str] = []
        self.closed = False

    async def instruments(self) -> list[InstrumentPair]:
        return self.pairs

    async def quotes(self, pairs: list[InstrumentPair]) -> list[MarketQuote]:
        now = datetime.now(UTC)
        return [
            MarketQuote(
                exchange=Exchange.GATE,
                base_asset=pair.base_asset,
                observed_at=now,
                spot_bid=Decimal("9.99"),
                spot_bid_qty=Decimal("0"),
                spot_ask=Decimal("10"),
                spot_ask_qty=Decimal("0"),
                perp_bid=Decimal("10.10"),
                perp_bid_qty=Decimal("20"),
                perp_ask=Decimal("10.11"),
                perp_ask_qty=Decimal("20"),
                spot_quote_volume_24h=Decimal("2000000"),
                perp_quote_volume_24h=Decimal("3000000"),
            )
            for pair in pairs
        ]

    async def current_funding(
        self,
        pairs: list[InstrumentPair],
    ) -> list[FundingObservation]:
        now = datetime.now(UTC)
        return [
            FundingObservation(
                exchange=Exchange.GATE,
                base_asset=pair.base_asset,
                rate=Decimal("0.0002"),
                funding_at=now,
                observed_at=now,
                interval_hours=Decimal("8"),
            )
            for pair in pairs
        ]

    async def funding_history(
        self,
        pair: InstrumentPair,
        *,
        start: datetime,
        end: datetime,
    ) -> list[FundingObservation]:
        return []

    async def executable_quote(self, pair, quote):
        self.calls.append(pair.base_asset)
        return quote.model_copy(
            update={
                "observed_at": datetime.now(UTC),
                "spot_bid": Decimal("9.99"),
                "spot_bid_qty": Decimal("20"),
                "spot_ask": Decimal("10"),
                "spot_ask_qty": Decimal("15"),
                "perp_bid": Decimal("10.10"),
                "perp_bid_qty": Decimal("20"),
                "perp_ask": Decimal("10.11"),
                "perp_ask_qty": Decimal("20"),
            }
        )

    async def close(self) -> None:
        self.closed = True


async def test_gate_automatic_service_fetches_bounded_sandbox_depth() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime.now(UTC)
    config = _config(
        environment="sandbox",
        enabled_exchanges={Exchange.GATE},
    )
    strategy = await database.create_strategy_version(
        environment="sandbox",
        payload=config.model_dump(mode="json"),
        actor="test",
    )
    await database.set_automation_control(
        state="enabled",
        active_strategy_id=strategy.id,
        reason="test",
        actor="test",
    )
    await database.set_execution_control(state="ready", reason="test")
    opportunities = [
        _opportunity(
            exchange=Exchange.GATE,
            base_asset=f"GATE{index:02d}",
            observed_at=now,
            net_return=Decimal("0.04") - Decimal(index) / Decimal("10000"),
        ).model_copy(
            update={
                "spot_symbol": f"GATE{index:02d}_USDT",
                "perp_symbol": f"GATE{index:02d}_USDT",
                "top_book_notional": Decimal("0"),
                "close_top_book_notional": Decimal("0"),
                "apr_24h": None,
                "apr_7d": None,
                "net_return": None,
                "quality": Quality.WARMING,
            }
        )
        for index in range(GATE_AUTOMATIC_DEPTH_CANDIDATES + 5)
    ]
    await database.save_latest_opportunities(opportunities)
    pairs = [
        InstrumentPair(
            exchange=Exchange.GATE,
            base_asset=item.base_asset,
            spot_symbol=f"{item.base_asset}_USDT",
            perp_symbol=f"{item.base_asset}_USDT",
            spot_price_increment=Decimal("0.01"),
            spot_quantity_increment=Decimal("0.01"),
            spot_min_quantity=Decimal("0.01"),
            spot_min_notional=Decimal("5"),
            perp_price_increment=Decimal("0.01"),
            perp_quantity_increment=Decimal("1"),
            perp_min_quantity=Decimal("1"),
            perp_min_notional=Decimal("5"),
            perp_contract_size=Decimal("0.01"),
        )
        for item in opportunities
    ]
    await database.replace_instruments("gate", pairs)
    sandbox_pairs = [
        pair.model_copy(
            update={"spot_quantity_increment": Decimal("0.4")}
        )
        if pair.base_asset == "GATE00"
        else pair
        for pair in pairs
    ]
    adapter = _GateDepthAdapter(sandbox_pairs)
    environments: list[ExchangeEnvironment] = []

    def gate_adapter_factory(environment: ExchangeEnvironment):
        environments.append(environment)
        return adapter

    result = await AutomaticTradingService(
        database,
        gate_adapter_factory=gate_adapter_factory,
    ).run_once()

    assert result.created is True
    assert result.action == "open"
    assert environments == [ExchangeEnvironment.SANDBOX]
    assert len(adapter.calls) == GATE_AUTOMATIC_DEPTH_CANDIDATES
    assert adapter.calls[0] == "GATE00"
    assert adapter.closed is True
    recoverable = await database.recoverable_trade_intents()
    assert len(recoverable) == 1
    assert recoverable[0].exchange == Exchange.GATE.value
    assert recoverable[0].environment == "sandbox"
    assert recoverable[0].base_asset == "GATE00"
    assert recoverable[0].requested_notional == Decimal("75")
    assert abs(
        recoverable[0].base_quantity - Decimal("7.2")
    ) < Decimal("0.000000000001")
    await database.close()
