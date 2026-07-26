"""Persist requested trade leverage.

Revision ID: 20260726_0012
Revises: 20260726_0011
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0012"
down_revision = "20260726_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_intents",
        sa.Column(
            "leverage",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    op.alter_column("trade_intents", "leverage", server_default=None)
    op.create_check_constraint(
        "ck_trade_intent_leverage_range",
        "trade_intents",
        "leverage >= 1 AND leverage <= 10",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_leverage_range",
        "trade_intents",
        type_="check",
    )
    op.drop_column("trade_intents", "leverage")
