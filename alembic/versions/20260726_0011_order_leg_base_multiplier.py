"""Persist order-leg base quantity multipliers.

Revision ID: 20260726_0011
Revises: 20260726_0010
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0011"
down_revision = "20260726_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_legs",
        sa.Column(
            "base_multiplier",
            sa.Numeric(precision=38, scale=18),
            server_default="1",
            nullable=False,
        ),
    )
    op.alter_column("order_legs", "base_multiplier", server_default=None)
    op.create_check_constraint(
        "ck_order_leg_base_multiplier_positive",
        "order_legs",
        "base_multiplier > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_order_leg_base_multiplier_positive",
        "order_legs",
        type_="check",
    )
    op.drop_column("order_legs", "base_multiplier")
