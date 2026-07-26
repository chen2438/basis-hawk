"""Add persistent paired trade ledger.

Revision ID: 20260726_0004
Revises: 20260726_0003
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0004"
down_revision = "20260726_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "requested_notional",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("base_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("market_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_trade_intents_exchange", "trade_intents", ["exchange"])
    op.create_index("ix_trade_intents_status", "trade_intents", ["status"])
    op.create_table(
        "order_legs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trade_intent_id", sa.String(length=36), nullable=False),
        sa.Column("leg", sa.String(length=20), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=100), nullable=False),
        sa.Column("side", sa.String(length=20), nullable=False),
        sa.Column("client_order_id", sa.String(length=100), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "filled_quantity",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "average_price",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["trade_intent_id"],
            ["trade_intents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint(
            "trade_intent_id",
            "leg",
            name="uq_order_leg_intent_leg",
        ),
    )
    op.create_index(
        "ix_order_legs_trade_intent_id",
        "order_legs",
        ["trade_intent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_legs_trade_intent_id", table_name="order_legs")
    op.drop_table("order_legs")
    op.drop_index("ix_trade_intents_status", table_name="trade_intents")
    op.drop_index("ix_trade_intents_exchange", table_name="trade_intents")
    op.drop_table("trade_intents")
