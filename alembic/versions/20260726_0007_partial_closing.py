"""Allow repeated partial closes and track remaining opening cost.

Revision ID: 20260726_0007
Revises: 20260726_0006
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0007"
down_revision = "20260726_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_trade_intents_paired_position_id",
        "trade_intents",
        type_="unique",
    )
    op.create_index(
        "ix_trade_intents_paired_position_id",
        "trade_intents",
        ["paired_position_id"],
    )
    op.add_column(
        "paired_positions",
        sa.Column(
            "initial_quantity",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "paired_positions",
        sa.Column(
            "remaining_opening_fees_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE paired_positions
        SET initial_quantity = quantity,
            remaining_opening_fees_usdt = opening_fees_usdt
        """
    )
    op.alter_column(
        "paired_positions",
        "initial_quantity",
        existing_type=sa.Numeric(precision=38, scale=18),
        nullable=False,
    )
    op.alter_column(
        "paired_positions",
        "remaining_opening_fees_usdt",
        existing_type=sa.Numeric(precision=38, scale=18),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("paired_positions", "remaining_opening_fees_usdt")
    op.drop_column("paired_positions", "initial_quantity")
    op.drop_index(
        "ix_trade_intents_paired_position_id",
        table_name="trade_intents",
    )
    op.create_unique_constraint(
        "uq_trade_intents_paired_position_id",
        "trade_intents",
        ["paired_position_id"],
    )
