"""Add paper fills and paired positions.

Revision ID: 20260726_0005
Revises: 20260726_0004
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0005"
down_revision = "20260726_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_intents",
        sa.Column(
            "spot_fee_rate",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "trade_intents",
        sa.Column(
            "perp_fee_rate",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_leg_id", sa.String(length=36), nullable=False),
        sa.Column("exchange_trade_id", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fee_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fee_asset", sa.String(length=40), nullable=False),
        sa.Column("liquidity", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_leg_id"],
            ["order_legs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange_trade_id"),
    )
    op.create_index("ix_fills_order_leg_id", "fills", ["order_leg_id"])
    op.create_table(
        "paired_positions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("opening_intent_id", sa.String(length=36), nullable=False),
        sa.Column("closing_intent_id", sa.String(length=36), nullable=True),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "spot_entry_price",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "perp_entry_price",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "opening_fees_usdt",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["closing_intent_id"],
            ["trade_intents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opening_intent_id"],
            ["trade_intents.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("closing_intent_id"),
        sa.UniqueConstraint("opening_intent_id"),
    )
    op.create_index(
        "ix_paired_positions_exchange",
        "paired_positions",
        ["exchange"],
    )
    op.create_index(
        "ix_paired_positions_status",
        "paired_positions",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_paired_positions_status", table_name="paired_positions")
    op.drop_index("ix_paired_positions_exchange", table_name="paired_positions")
    op.drop_table("paired_positions")
    op.drop_index("ix_fills_order_leg_id", table_name="fills")
    op.drop_table("fills")
    op.drop_column("trade_intents", "perp_fee_rate")
    op.drop_column("trade_intents", "spot_fee_rate")
