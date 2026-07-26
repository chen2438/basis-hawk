"""Record idempotent realized PnL events for every completed close.

Revision ID: 20260726_0018
Revises: 20260726_0017
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0018"
down_revision = "20260726_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pnl_realizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paired_position_id", sa.String(length=36), nullable=False),
        sa.Column("closing_intent_id", sa.String(length=36), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "gross_pnl_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "opening_fee_allocated_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "closing_fees_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "net_pnl_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("realized_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "closing_fees_usdt >= 0",
            name="ck_pnl_realization_closing_fees_nonnegative",
        ),
        sa.CheckConstraint(
            "opening_fee_allocated_usdt >= 0",
            name="ck_pnl_realization_opening_fee_nonnegative",
        ),
        sa.CheckConstraint(
            "quantity >= 0",
            name="ck_pnl_realization_quantity_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["closing_intent_id"],
            ["trade_intents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paired_position_id"],
            ["paired_positions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("closing_intent_id"),
    )
    op.create_index(
        op.f("ix_pnl_realizations_paired_position_id"),
        "pnl_realizations",
        ["paired_position_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pnl_realizations_realized_at"),
        "pnl_realizations",
        ["realized_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pnl_realizations_realized_at"),
        table_name="pnl_realizations",
    )
    op.drop_index(
        op.f("ix_pnl_realizations_paired_position_id"),
        table_name="pnl_realizations",
    )
    op.drop_table("pnl_realizations")
