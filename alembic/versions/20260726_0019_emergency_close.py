"""Mark emergency close previews and intents.

Revision ID: 20260726_0019
Revises: 20260726_0018
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0019"
down_revision = "20260726_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_previews",
        sa.Column(
            "emergency",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.drop_constraint(
        "ck_trade_preview_slippage_range",
        "trade_previews",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_preview_slippage_range",
        "trade_previews",
        "maximum_slippage > 0 AND maximum_slippage <= 0.25",
    )
    op.create_check_constraint(
        "ck_trade_preview_emergency_close",
        "trade_previews",
        "emergency = false OR action = 'close'",
    )
    op.add_column(
        "trade_intents",
        sa.Column(
            "emergency",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "ck_trade_intent_emergency_close",
        "trade_intents",
        "emergency = false OR action = 'close'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_emergency_close",
        "trade_intents",
        type_="check",
    )
    op.drop_column("trade_intents", "emergency")
    op.drop_constraint(
        "ck_trade_preview_emergency_close",
        "trade_previews",
        type_="check",
    )
    op.drop_constraint(
        "ck_trade_preview_slippage_range",
        "trade_previews",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_preview_slippage_range",
        "trade_previews",
        "maximum_slippage > 0 AND maximum_slippage <= 0.1",
    )
    op.drop_column("trade_previews", "emergency")
