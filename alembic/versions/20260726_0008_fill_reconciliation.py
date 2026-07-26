"""Persist scoped remote fills and reconciliation completeness.

Revision ID: 20260726_0008
Revises: 20260726_0007
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0008"
down_revision = "20260726_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fills_exchange_trade_id_key",
        "fills",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_fill_leg_exchange_trade",
        "fills",
        ["order_leg_id", "exchange_trade_id"],
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "fill_reconciliation_complete",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "fill_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.alter_column(
        "account_reconciliation",
        "fill_reconciliation_complete",
        server_default=None,
    )
    op.alter_column(
        "account_reconciliation",
        "fill_count",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("account_reconciliation", "fill_count")
    op.drop_column(
        "account_reconciliation",
        "fill_reconciliation_complete",
    )
    op.drop_constraint(
        "uq_fill_leg_exchange_trade",
        "fills",
        type_="unique",
    )
    op.create_unique_constraint(
        "fills_exchange_trade_id_key",
        "fills",
        ["exchange_trade_id"],
    )
