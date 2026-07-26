"""Add paper closing links and realized PnL.

Revision ID: 20260726_0006
Revises: 20260726_0005
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0006"
down_revision = "20260726_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_intents",
        sa.Column("paired_position_id", sa.String(length=36), nullable=True),
    )
    op.create_unique_constraint(
        "uq_trade_intents_paired_position_id",
        "trade_intents",
        ["paired_position_id"],
    )
    op.create_foreign_key(
        "fk_trade_intents_paired_position_id",
        "trade_intents",
        "paired_positions",
        ["paired_position_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "paired_positions",
        sa.Column(
            "closing_fees_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "paired_positions",
        sa.Column(
            "realized_pnl_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("paired_positions", "realized_pnl_usdt")
    op.drop_column("paired_positions", "closing_fees_usdt")
    op.drop_constraint(
        "fk_trade_intents_paired_position_id",
        "trade_intents",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_trade_intents_paired_position_id",
        "trade_intents",
        type_="unique",
    )
    op.drop_column("trade_intents", "paired_position_id")
