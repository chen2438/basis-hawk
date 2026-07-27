"""Persist safe trade-intent failure codes.

Revision ID: 20260727_0027
Revises: 20260727_0026
"""

import sqlalchemy as sa

from alembic import op

revision = "20260727_0027"
down_revision = "20260727_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_intents",
        sa.Column("failure_code", sa.String(length=50), nullable=True),
    )
    op.execute(
        """
        UPDATE trade_intents AS intent
        SET failure_code = 'market_data_expired'
        WHERE intent.status = 'failed'
          AND intent.environment IN ('sandbox', 'live')
          AND (
            SELECT COUNT(*)
            FROM order_legs AS leg
            WHERE leg.trade_intent_id = intent.id
          ) = 2
          AND NOT EXISTS (
            SELECT 1
            FROM order_legs AS leg
            WHERE leg.trade_intent_id = intent.id
              AND leg.status <> 'created'
          )
        """
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        "failure_code IS NULL OR failure_code IN "
        "('market_data_expired', 'no_fills', "
        "'exposure_neutralized', 'state_transition_failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        type_="check",
    )
    op.drop_column("trade_intents", "failure_code")
