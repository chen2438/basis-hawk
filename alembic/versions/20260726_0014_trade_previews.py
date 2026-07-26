"""Persist confirmed live trade previews.

Revision ID: 20260726_0014
Revises: 20260726_0013
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0014"
down_revision = "20260726_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_previews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column(
            "requested_notional",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column(
            "maximum_slippage",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "market_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "confirmation_idempotency_key",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "leverage >= 1 AND leverage <= 10",
            name="ck_trade_preview_leverage_range",
        ),
        sa.CheckConstraint(
            "maximum_slippage > 0 AND maximum_slippage <= 0.1",
            name="ck_trade_preview_slippage_range",
        ),
        sa.CheckConstraint(
            "requested_notional > 0",
            name="ck_trade_preview_notional_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_idempotency_key"),
    )
    op.create_index(
        op.f("ix_trade_previews_exchange"),
        "trade_previews",
        ["exchange"],
        unique=False,
    )
    op.create_index(
        op.f("ix_trade_previews_expires_at"),
        "trade_previews",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_trade_previews_expires_at"),
        table_name="trade_previews",
    )
    op.drop_index(
        op.f("ix_trade_previews_exchange"),
        table_name="trade_previews",
    )
    op.drop_table("trade_previews")
