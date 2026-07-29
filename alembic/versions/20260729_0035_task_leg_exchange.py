"""Persist the exchange on every execution task leg.

Revision ID: 20260729_0035
Revises: 20260729_0034
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_0035"
down_revision = "20260729_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_task_legs",
        sa.Column("exchange", sa.String(length=20), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE execution_task_legs AS leg
            SET exchange = credential.exchange
            FROM exchange_credentials AS credential
            WHERE leg.account_id = credential.id
              AND leg.exchange IS NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE execution_task_legs AS leg
            SET exchange = intent.exchange
            FROM trade_intents AS intent
            WHERE leg.task_id = intent.id
              AND leg.exchange IS NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE execution_task_legs AS leg
            SET exchange = candidate.exchange
            FROM (
                SELECT target.id, min(instrument.exchange) AS exchange
                FROM execution_task_legs AS target
                JOIN instruments AS instrument
                  ON instrument.base_asset = target.base_asset
                 AND (
                    (
                        target.market_type = 'spot'
                        AND instrument.spot_symbol = target.symbol
                    )
                    OR (
                        target.market_type = 'perpetual'
                        AND instrument.perp_symbol = target.symbol
                    )
                 )
                WHERE target.exchange IS NULL
                GROUP BY target.id
                HAVING count(DISTINCT instrument.exchange) = 1
            ) AS candidate
            WHERE leg.id = candidate.id
              AND leg.exchange IS NULL
            """
        )
    )
    unresolved = connection.scalar(
        sa.text(
            "SELECT count(*) FROM execution_task_legs WHERE exchange IS NULL"
        )
    )
    if unresolved:
        raise RuntimeError(
            "task-leg exchange migration cannot safely infer every paper leg; "
            "remove unstarted ambiguous tasks or bind them to an account"
        )
    op.alter_column(
        "execution_task_legs",
        "exchange",
        existing_type=sa.String(length=20),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_execution_task_leg_exchange",
        "execution_task_legs",
        "exchange IN ('binance', 'okx', 'mexc', 'bybit', 'bitget', 'gate')",
    )
    op.create_index(
        "ix_execution_task_legs_exchange",
        "execution_task_legs",
        ["exchange"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_task_legs_exchange",
        table_name="execution_task_legs",
    )
    op.drop_constraint(
        "ck_execution_task_leg_exchange",
        "execution_task_legs",
        type_="check",
    )
    op.drop_column("execution_task_legs", "exchange")
