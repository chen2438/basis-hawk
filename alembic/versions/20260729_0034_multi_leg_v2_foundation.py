"""Add the generic multi-account, multi-leg execution foundation.

Revision ID: 20260729_0034
Revises: 20260729_0033
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_0034"
down_revision = "20260729_0033"
branch_labels = None
depends_on = None


DECIMAL = sa.Numeric(38, 18)
TIMESTAMP = sa.DateTime(timezone=True)


def _require_flat_legacy_state(connection: sa.Connection) -> None:
    active_positions = connection.scalar(
        sa.text(
            "SELECT count(*) FROM paired_positions "
            "WHERE status <> 'closed' OR quantity <> 0"
        )
    )
    active_orders = connection.scalar(
        sa.text(
            "SELECT count(*) FROM order_legs WHERE status IN "
            "('created', 'submitted', 'acknowledged', 'partially_filled', 'unknown')"
        )
    )
    active_intents = connection.scalar(
        sa.text(
            "SELECT count(*) FROM trade_intents WHERE status IN "
            "('planned', 'executing', 'closing', 'compensating', 'manual_review')"
        )
    )
    if active_positions or active_orders or active_intents:
        raise RuntimeError(
            "multi-leg v2 migration requires all legacy positions, orders, "
            "and executable intents to be terminal"
        )


def _upgrade_credentials() -> None:
    op.add_column(
        "exchange_credentials",
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "exchange_credentials",
        sa.Column(
            "scanner_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "exchange_credentials",
        sa.Column(
            "capabilities_payload",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "exchange_credentials",
        sa.Column(
            "fee_payload",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.execute(
        "UPDATE exchange_credentials "
        "SET is_default = true, scanner_default = true"
    )
    op.drop_constraint(
        "uq_exchange_credential",
        "exchange_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_exchange_credential_label",
        "exchange_credentials",
        ["exchange", "environment", "label"],
    )
    op.create_index(
        "uq_exchange_credential_default",
        "exchange_credentials",
        ["exchange", "environment"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_exchange_credential_scanner_default",
        "exchange_credentials",
        ["exchange", "environment"],
        unique=True,
        postgresql_where=sa.text("scanner_default"),
    )
    for column in (
        "is_default",
        "scanner_default",
        "capabilities_payload",
        "fee_payload",
    ):
        op.alter_column("exchange_credentials", column, server_default=None)


def _create_execution_tables() -> None:
    op.create_table(
        "execution_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(36), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("display_symbol", sa.String(100), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("base_asset", sa.String(40), nullable=False),
        sa.Column("quantity_mode", sa.String(20), nullable=False),
        sa.Column("source_opportunity_id", sa.String(100), nullable=True),
        sa.Column("create_strategy", sa.Boolean(), nullable=False),
        sa.Column("hedge_trigger", sa.String(30), nullable=False),
        sa.Column("hedge_threshold", DECIMAL, nullable=True),
        sa.Column("maximum_base_exposure", DECIMAL, nullable=False),
        sa.Column("maximum_notional_exposure_usdt", DECIMAL, nullable=False),
        sa.Column("maximum_retries", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("preflight_payload", sa.Text(), nullable=True),
        sa.Column("preflight_expires_at", TIMESTAMP, nullable=True),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.CheckConstraint(
            "environment IN ('paper', 'sandbox', 'live')",
            name="ck_execution_task_environment",
        ),
        sa.CheckConstraint(
            "quantity_mode IN ('base', 'usdt')",
            name="ck_execution_task_quantity_mode",
        ),
        sa.CheckConstraint(
            "hedge_trigger IN ('realtime', 'cumulative_percent')",
            name="ck_execution_task_hedge_trigger",
        ),
        sa.CheckConstraint(
            "(hedge_trigger = 'realtime' AND hedge_threshold IS NULL) OR "
            "(hedge_trigger = 'cumulative_percent' AND "
            "hedge_threshold > 0 AND hedge_threshold <= 1)",
            name="ck_execution_task_hedge_threshold",
        ),
        sa.CheckConstraint(
            "maximum_base_exposure > 0",
            name="ck_execution_task_base_exposure_positive",
        ),
        sa.CheckConstraint(
            "maximum_notional_exposure_usdt > 0",
            name="ck_execution_task_notional_exposure_positive",
        ),
        sa.CheckConstraint(
            "maximum_retries >= 0 AND maximum_retries <= 20",
            name="ck_execution_task_retries_range",
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_task_version_positive"),
    )
    op.create_index(
        "ix_execution_tasks_environment",
        "execution_tasks",
        ["environment"],
    )
    op.create_index(
        "ix_execution_tasks_base_asset",
        "execution_tasks",
        ["base_asset"],
    )
    op.create_index("ix_execution_tasks_status", "execution_tasks", ["status"])

    op.create_table(
        "execution_task_legs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("execution_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("exchange_credentials.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("market_type", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("base_asset", sa.String(40), nullable=False),
        sa.Column("quote_asset", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(100), nullable=False),
        sa.Column("target_quantity", DECIMAL, nullable=False),
        sa.Column("resolved_base_quantity", DECIMAL, nullable=True),
        sa.Column("signed_base_ratio", DECIMAL, nullable=True),
        sa.Column("per_order_quantity", DECIMAL, nullable=False),
        sa.Column("order_mode", sa.String(30), nullable=False),
        sa.Column("maximum_slippage", DECIMAL, nullable=False),
        sa.Column("maker_book_level", sa.Integer(), nullable=True),
        sa.Column("maker_maximum_chases", sa.Integer(), nullable=True),
        sa.Column("maker_fallback_mode", sa.String(30), nullable=True),
        sa.Column("margin_mode", sa.String(20), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "ordinal",
            name="uq_execution_task_leg_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal >= 0 AND ordinal < 64",
            name="ck_execution_task_leg_ordinal_range",
        ),
        sa.CheckConstraint(
            "role IN ('anchor', 'hedge')",
            name="ck_execution_task_leg_role",
        ),
        sa.CheckConstraint(
            "market_type IN ('spot', 'perpetual')",
            name="ck_execution_task_leg_market",
        ),
        sa.CheckConstraint(
            "side IN ('buy', 'sell')",
            name="ck_execution_task_leg_side",
        ),
        sa.CheckConstraint(
            "quote_asset = 'USDT'",
            name="ck_execution_task_leg_usdt_only",
        ),
        sa.CheckConstraint(
            "target_quantity > 0",
            name="ck_execution_task_leg_target_positive",
        ),
        sa.CheckConstraint(
            "per_order_quantity >= 0 AND per_order_quantity <= target_quantity",
            name="ck_execution_task_leg_child_quantity",
        ),
        sa.CheckConstraint(
            "order_mode IN ('maker', 'protected_ioc', 'market')",
            name="ck_execution_task_leg_order_mode",
        ),
        sa.CheckConstraint(
            "maximum_slippage > 0 AND maximum_slippage <= 0.25",
            name="ck_execution_task_leg_slippage",
        ),
        sa.CheckConstraint(
            "(order_mode = 'maker' AND maker_book_level BETWEEN 1 AND 20 "
            "AND maker_maximum_chases BETWEEN 0 AND 200) OR "
            "(order_mode <> 'maker' AND maker_book_level IS NULL "
            "AND maker_maximum_chases IS NULL AND maker_fallback_mode IS NULL)",
            name="ck_execution_task_leg_maker_policy",
        ),
        sa.CheckConstraint(
            "maker_fallback_mode IS NULL OR "
            "maker_fallback_mode IN ('protected_ioc', 'market')",
            name="ck_execution_task_leg_maker_fallback",
        ),
        sa.CheckConstraint(
            "(market_type = 'spot' AND margin_mode IS NULL AND leverage = 1 "
            "AND reduce_only = false) OR "
            "(market_type = 'perpetual' AND "
            "margin_mode IN ('isolated', 'cross') AND leverage BETWEEN 1 AND 10)",
            name="ck_execution_task_leg_margin",
        ),
    )
    op.create_index(
        "ix_execution_task_legs_task_id",
        "execution_task_legs",
        ["task_id"],
    )
    op.create_index(
        "ix_execution_task_legs_account_id",
        "execution_task_legs",
        ["account_id"],
    )

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("execution_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=True),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "run_number",
            name="uq_execution_run_number",
        ),
        sa.CheckConstraint(
            "run_number >= 1",
            name="ck_execution_run_number_positive",
        ),
    )
    op.create_index("ix_execution_runs_task_id", "execution_runs", ["task_id"])
    op.create_index("ix_execution_runs_status", "execution_runs", ["status"])

    op.create_table(
        "execution_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_leg_id",
            sa.String(36),
            sa.ForeignKey("execution_task_legs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_order_id",
            sa.String(36),
            sa.ForeignKey("execution_orders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("chase_number", sa.Integer(), nullable=False),
        sa.Column("client_order_id", sa.String(100), nullable=False, unique=True),
        sa.Column("exchange_order_id", sa.String(100), nullable=True),
        sa.Column("order_mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quantity", DECIMAL, nullable=False),
        sa.Column("base_multiplier", DECIMAL, nullable=False),
        sa.Column("limit_price", DECIMAL, nullable=True),
        sa.Column("filled_quantity", DECIMAL, nullable=False),
        sa.Column("average_price", DECIMAL, nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("submitted_at", TIMESTAMP, nullable=True),
        sa.Column("terminal_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint(
            "task_leg_id",
            "attempt_number",
            "chase_number",
            name="uq_execution_order_attempt_chase",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1 AND chase_number >= 0",
            name="ck_execution_order_attempt",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND base_multiplier > 0",
            name="ck_execution_order_quantity",
        ),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_execution_order_filled_quantity",
        ),
    )
    op.create_index("ix_execution_orders_run_id", "execution_orders", ["run_id"])
    op.create_index(
        "ix_execution_orders_task_leg_id",
        "execution_orders",
        ["task_leg_id"],
    )
    op.create_index("ix_execution_orders_status", "execution_orders", ["status"])

    op.create_table(
        "execution_fills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "execution_order_id",
            sa.String(36),
            sa.ForeignKey("execution_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exchange_trade_id", sa.String(120), nullable=False),
        sa.Column("quantity", DECIMAL, nullable=False),
        sa.Column("price", DECIMAL, nullable=False),
        sa.Column("fee_amount", DECIMAL, nullable=False),
        sa.Column("fee_asset", sa.String(40), nullable=False),
        sa.Column("liquidity", sa.String(20), nullable=False),
        sa.Column("occurred_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint(
            "execution_order_id",
            "exchange_trade_id",
            name="uq_execution_fill_remote_trade",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0",
            name="ck_execution_fill_quantity_price",
        ),
    )
    op.create_index(
        "ix_execution_fills_execution_order_id",
        "execution_fills",
        ["execution_order_id"],
    )
    op.create_index(
        "ix_execution_fills_occurred_at",
        "execution_fills",
        ["occurred_at"],
    )


def _create_strategy_and_risk_tables() -> None:
    op.create_table(
        "arbitrage_strategies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("base_asset", sa.String(40), nullable=False),
        sa.Column(
            "opening_task_id",
            sa.String(36),
            sa.ForeignKey("execution_tasks.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "closing_task_id",
            sa.String(36),
            sa.ForeignKey("execution_tasks.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("realized_pnl_usdt", DECIMAL, nullable=False),
        sa.Column("funding_income_usdt", DECIMAL, nullable=False),
        sa.Column("fees_usdt", DECIMAL, nullable=False),
        sa.Column("opened_at", TIMESTAMP, nullable=False),
        sa.Column("closed_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
    )
    op.create_index(
        "ix_arbitrage_strategies_environment",
        "arbitrage_strategies",
        ["environment"],
    )
    op.create_index(
        "ix_arbitrage_strategies_base_asset",
        "arbitrage_strategies",
        ["base_asset"],
    )
    op.create_index(
        "ix_arbitrage_strategies_status",
        "arbitrage_strategies",
        ["status"],
    )

    op.create_table(
        "strategy_legs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "strategy_id",
            sa.String(36),
            sa.ForeignKey("arbitrage_strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opening_task_leg_id",
            sa.String(36),
            sa.ForeignKey("execution_task_legs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("exchange_credentials.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("market_type", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(100), nullable=False),
        sa.Column("initial_base_quantity", DECIMAL, nullable=False),
        sa.Column("remaining_base_quantity", DECIMAL, nullable=False),
        sa.Column("entry_price", DECIMAL, nullable=False),
        sa.Column("exit_price", DECIMAL, nullable=True),
        sa.Column("fees_usdt", DECIMAL, nullable=False),
        sa.Column("realized_pnl_usdt", DECIMAL, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint(
            "strategy_id",
            "ordinal",
            name="uq_strategy_leg_ordinal",
        ),
        sa.CheckConstraint(
            "initial_base_quantity > 0 AND remaining_base_quantity >= 0 "
            "AND remaining_base_quantity <= initial_base_quantity",
            name="ck_strategy_leg_quantity",
        ),
    )
    op.create_index("ix_strategy_legs_strategy_id", "strategy_legs", ["strategy_id"])

    op.create_table(
        "strategy_pnl_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "strategy_id",
            sa.String(36),
            sa.ForeignKey("arbitrage_strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "closing_task_id",
            sa.String(36),
            sa.ForeignKey("execution_tasks.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("quantity", DECIMAL, nullable=False),
        sa.Column("gross_pnl_usdt", DECIMAL, nullable=False),
        sa.Column("opening_fee_allocated_usdt", DECIMAL, nullable=False),
        sa.Column("closing_fees_usdt", DECIMAL, nullable=False),
        sa.Column("net_pnl_usdt", DECIMAL, nullable=False),
        sa.Column("realized_at", TIMESTAMP, nullable=False),
        sa.CheckConstraint(
            "quantity >= 0 AND opening_fee_allocated_usdt >= 0 "
            "AND closing_fees_usdt >= 0",
            name="ck_strategy_pnl_event_nonnegative",
        ),
    )
    op.create_index(
        "ix_strategy_pnl_events_strategy_id",
        "strategy_pnl_events",
        ["strategy_id"],
    )
    op.create_index(
        "ix_strategy_pnl_events_realized_at",
        "strategy_pnl_events",
        ["realized_at"],
    )

    op.create_table(
        "adl_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("exchange_credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(100), nullable=False),
        sa.Column("position_side", sa.String(20), nullable=False),
        sa.Column("normalized_level", sa.Integer(), nullable=True),
        sa.Column("native_value", sa.String(100), nullable=True),
        sa.Column("event_only", sa.Boolean(), nullable=False),
        sa.Column("observed_at", TIMESTAMP, nullable=False),
        sa.CheckConstraint(
            "normalized_level IS NULL OR "
            "(normalized_level >= 1 AND normalized_level <= 5)",
            name="ck_adl_snapshot_level",
        ),
    )
    op.create_index("ix_adl_snapshots_account_id", "adl_snapshots", ["account_id"])
    op.create_index("ix_adl_snapshots_observed_at", "adl_snapshots", ["observed_at"])
    op.create_index(
        "ix_adl_snapshot_account_symbol",
        "adl_snapshots",
        ["account_id", "symbol", "observed_at"],
    )

    op.add_column(
        "funding_income",
        sa.Column("account_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "funding_income",
        sa.Column("strategy_leg_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_funding_income_account",
        "funding_income",
        "exchange_credentials",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_funding_income_strategy_leg",
        "funding_income",
        "strategy_legs",
        ["strategy_leg_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_funding_income_account_id", "funding_income", ["account_id"])
    op.create_index(
        "ix_funding_income_strategy_leg_id",
        "funding_income",
        ["strategy_leg_id"],
    )


def _migrate_legacy_history(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_tasks (
                id, idempotency_key, request_fingerprint, name, display_symbol,
                environment, base_asset, quantity_mode, source_opportunity_id,
                create_strategy, hedge_trigger, hedge_threshold,
                maximum_base_exposure, maximum_notional_exposure_usdt,
                maximum_retries, status, failure_code, preflight_payload,
                preflight_expires_at, created_by, version, created_at, updated_at
            )
            SELECT
                id, idempotency_key, request_fingerprint,
                base_asset || ' legacy ' || action,
                base_asset || '/USDT',
                environment, base_asset, 'base', NULL, false, 'realtime', NULL,
                CASE WHEN base_quantity > 0 THEN base_quantity ELSE 0.000000000000000001 END,
                CASE WHEN requested_notional > 0
                    THEN requested_notional ELSE 0.000000000000000001 END,
                0,
                CASE
                    WHEN status IN ('hedged', 'closed') THEN 'completed'
                    WHEN status = 'manual_review' THEN 'manual_review'
                    ELSE 'failed'
                END,
                failure_code, NULL, NULL, 'legacy-migration', 1,
                created_at, updated_at
            FROM trade_intents
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_task_legs (
                id, task_id, account_id, ordinal, role, market_type, side,
                base_asset, quote_asset, symbol, target_quantity,
                resolved_base_quantity, signed_base_ratio, per_order_quantity,
                order_mode, maximum_slippage, maker_book_level,
                maker_maximum_chases, maker_fallback_mode, margin_mode,
                leverage, reduce_only, created_at, updated_at
            )
            SELECT
                leg.id,
                leg.trade_intent_id,
                (
                    SELECT cred.id
                    FROM exchange_credentials AS cred
                    WHERE cred.exchange = intent.exchange
                      AND cred.environment = intent.environment
                      AND cred.is_default = true
                    LIMIT 1
                ),
                CASE leg.leg WHEN 'spot' THEN 0 WHEN 'perp' THEN 1 ELSE 2 END,
                CASE WHEN leg.leg = 'spot' THEN 'anchor' ELSE 'hedge' END,
                CASE WHEN leg.market = 'perp' THEN 'perpetual' ELSE 'spot' END,
                leg.side,
                intent.base_asset,
                'USDT',
                leg.symbol,
                CASE
                    WHEN leg.quantity * leg.base_multiplier > 0
                    THEN leg.quantity * leg.base_multiplier
                    ELSE 0.000000000000000001
                END,
                CASE
                    WHEN leg.quantity * leg.base_multiplier > 0
                    THEN leg.quantity * leg.base_multiplier
                    ELSE 0.000000000000000001
                END,
                CASE
                    WHEN intent.base_quantity > 0
                    THEN (
                        CASE WHEN leg.side = 'buy' THEN 1 ELSE -1 END
                        * leg.quantity * leg.base_multiplier / intent.base_quantity
                    )
                    ELSE NULL
                END,
                0,
                'protected_ioc',
                0.25,
                NULL,
                NULL,
                NULL,
                CASE WHEN leg.market = 'perp' THEN 'isolated' ELSE NULL END,
                CASE WHEN leg.market = 'perp' THEN intent.leverage ELSE 1 END,
                CASE WHEN leg.market = 'perp' THEN leg.reduce_only ELSE false END,
                leg.created_at,
                leg.updated_at
            FROM order_legs AS leg
            JOIN trade_intents AS intent ON intent.id = leg.trade_intent_id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_runs (
                id, task_id, run_number, status, worker_id, failure_code,
                started_at, finished_at, created_at, updated_at
            )
            SELECT
                id, id, 1,
                CASE
                    WHEN status IN ('hedged', 'closed') THEN 'completed'
                    WHEN status = 'manual_review' THEN 'manual_review'
                    ELSE 'failed'
                END,
                'legacy-migration', failure_code, created_at, updated_at,
                created_at, updated_at
            FROM trade_intents
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_orders (
                id, run_id, task_leg_id, parent_order_id, attempt_number,
                chase_number, client_order_id, exchange_order_id, order_mode,
                status, quantity, base_multiplier, limit_price, filled_quantity,
                average_price, failure_code, submitted_at, terminal_at,
                created_at, updated_at
            )
            SELECT
                id, trade_intent_id, id, NULL, 1, 0, client_order_id,
                exchange_order_id, 'protected_ioc',
                CASE WHEN status = 'failed' THEN 'failed' ELSE status END,
                quantity, base_multiplier, limit_price, filled_quantity,
                average_price, failure_code,
                CASE WHEN status = 'created' THEN NULL ELSE created_at END,
                CASE
                    WHEN status IN ('filled', 'canceled', 'failed')
                    THEN updated_at ELSE NULL
                END,
                created_at, updated_at
            FROM order_legs
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_fills (
                id, execution_order_id, exchange_trade_id, quantity, price,
                fee_amount, fee_asset, liquidity, occurred_at
            )
            SELECT
                id, order_leg_id, exchange_trade_id, quantity, price,
                fee_amount, fee_asset, liquidity, occurred_at
            FROM fills
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO arbitrage_strategies (
                id, name, environment, base_asset, opening_task_id,
                closing_task_id, status, realized_pnl_usdt,
                funding_income_usdt, fees_usdt, opened_at, closed_at,
                created_at, updated_at
            )
            SELECT
                position.id,
                position.base_asset || ' legacy strategy',
                position.environment,
                position.base_asset,
                position.opening_intent_id,
                position.closing_intent_id,
                'ended',
                COALESCE(position.realized_pnl_usdt, 0),
                0,
                position.opening_fees_usdt
                    + COALESCE(position.closing_fees_usdt, 0),
                position.opened_at,
                position.closed_at,
                position.opened_at,
                COALESCE(position.closed_at, position.opened_at)
            FROM paired_positions AS position
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO strategy_legs (
                id, strategy_id, opening_task_leg_id, account_id, ordinal,
                market_type, side, symbol, initial_base_quantity,
                remaining_base_quantity, entry_price, exit_price, fees_usdt,
                realized_pnl_usdt, created_at, updated_at
            )
            SELECT
                leg.id,
                position.id,
                leg.id,
                task_leg.account_id,
                CASE leg.leg WHEN 'spot' THEN 0 ELSE 1 END,
                CASE WHEN leg.market = 'perp' THEN 'perpetual' ELSE 'spot' END,
                leg.side,
                leg.symbol,
                position.initial_quantity,
                0,
                CASE
                    WHEN leg.market = 'perp' THEN position.perp_entry_price
                    ELSE position.spot_entry_price
                END,
                (
                    SELECT close_leg.average_price
                    FROM order_legs AS close_leg
                    WHERE close_leg.trade_intent_id = position.closing_intent_id
                      AND close_leg.market = leg.market
                    LIMIT 1
                ),
                0,
                0,
                position.opened_at,
                COALESCE(position.closed_at, position.opened_at)
            FROM paired_positions AS position
            JOIN order_legs AS leg
              ON leg.trade_intent_id = position.opening_intent_id
             AND leg.leg IN ('spot', 'perp')
            JOIN execution_task_legs AS task_leg ON task_leg.id = leg.id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO strategy_pnl_events (
                id, strategy_id, closing_task_id, quantity, gross_pnl_usdt,
                opening_fee_allocated_usdt, closing_fees_usdt, net_pnl_usdt,
                realized_at
            )
            SELECT
                realization.id,
                realization.paired_position_id,
                realization.closing_intent_id,
                realization.quantity,
                realization.gross_pnl_usdt,
                realization.opening_fee_allocated_usdt,
                realization.closing_fees_usdt,
                realization.net_pnl_usdt,
                realization.realized_at
            FROM pnl_realizations AS realization
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE funding_income AS funding
            SET account_id = (
                SELECT credential.id
                FROM exchange_credentials AS credential
                WHERE credential.exchange = funding.exchange
                  AND credential.environment = funding.environment
                  AND credential.is_default = true
                LIMIT 1
            )
            """
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    _require_flat_legacy_state(connection)
    _upgrade_credentials()
    _create_execution_tables()
    _create_strategy_and_risk_tables()
    _migrate_legacy_history(connection)


def downgrade() -> None:
    op.drop_index("ix_funding_income_strategy_leg_id", table_name="funding_income")
    op.drop_index("ix_funding_income_account_id", table_name="funding_income")
    op.drop_constraint(
        "fk_funding_income_strategy_leg",
        "funding_income",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_funding_income_account",
        "funding_income",
        type_="foreignkey",
    )
    op.drop_column("funding_income", "strategy_leg_id")
    op.drop_column("funding_income", "account_id")

    op.drop_index("ix_adl_snapshot_account_symbol", table_name="adl_snapshots")
    op.drop_index("ix_adl_snapshots_observed_at", table_name="adl_snapshots")
    op.drop_index("ix_adl_snapshots_account_id", table_name="adl_snapshots")
    op.drop_table("adl_snapshots")
    op.drop_index(
        "ix_strategy_pnl_events_realized_at",
        table_name="strategy_pnl_events",
    )
    op.drop_index(
        "ix_strategy_pnl_events_strategy_id",
        table_name="strategy_pnl_events",
    )
    op.drop_table("strategy_pnl_events")
    op.drop_index("ix_strategy_legs_strategy_id", table_name="strategy_legs")
    op.drop_table("strategy_legs")
    op.drop_index(
        "ix_arbitrage_strategies_status",
        table_name="arbitrage_strategies",
    )
    op.drop_index(
        "ix_arbitrage_strategies_base_asset",
        table_name="arbitrage_strategies",
    )
    op.drop_index(
        "ix_arbitrage_strategies_environment",
        table_name="arbitrage_strategies",
    )
    op.drop_table("arbitrage_strategies")

    op.drop_index("ix_execution_fills_occurred_at", table_name="execution_fills")
    op.drop_index(
        "ix_execution_fills_execution_order_id",
        table_name="execution_fills",
    )
    op.drop_table("execution_fills")
    op.drop_index("ix_execution_orders_status", table_name="execution_orders")
    op.drop_index(
        "ix_execution_orders_task_leg_id",
        table_name="execution_orders",
    )
    op.drop_index("ix_execution_orders_run_id", table_name="execution_orders")
    op.drop_table("execution_orders")
    op.drop_index("ix_execution_runs_status", table_name="execution_runs")
    op.drop_index("ix_execution_runs_task_id", table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_index(
        "ix_execution_task_legs_account_id",
        table_name="execution_task_legs",
    )
    op.drop_index(
        "ix_execution_task_legs_task_id",
        table_name="execution_task_legs",
    )
    op.drop_table("execution_task_legs")
    op.drop_index("ix_execution_tasks_status", table_name="execution_tasks")
    op.drop_index("ix_execution_tasks_base_asset", table_name="execution_tasks")
    op.drop_index("ix_execution_tasks_environment", table_name="execution_tasks")
    op.drop_table("execution_tasks")

    op.drop_index(
        "uq_exchange_credential_scanner_default",
        table_name="exchange_credentials",
    )
    op.drop_index(
        "uq_exchange_credential_default",
        table_name="exchange_credentials",
    )
    op.drop_constraint(
        "uq_exchange_credential_label",
        "exchange_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_exchange_credential",
        "exchange_credentials",
        ["exchange", "environment"],
    )
    op.drop_column("exchange_credentials", "fee_payload")
    op.drop_column("exchange_credentials", "capabilities_payload")
    op.drop_column("exchange_credentials", "scanner_default")
    op.drop_column("exchange_credentials", "is_default")
