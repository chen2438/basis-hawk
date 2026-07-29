"""Persist executable compensation quantities and bounded base dust.

Revision ID: 20260729_0031
Revises: 20260729_0030
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_0031"
down_revision = "20260729_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_legs",
        sa.Column(
            "compensation_target_base_quantity",
            sa.Numeric(38, 18),
            nullable=True,
        ),
    )
    op.add_column(
        "order_legs",
        sa.Column(
            "compensation_tolerance_base",
            sa.Numeric(38, 18),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_order_leg_compensation_tolerance_non_negative",
        "order_legs",
        "compensation_tolerance_base >= 0",
    )
    op.execute(
        """
        UPDATE order_legs
        SET compensation_target_base_quantity = quantity * base_multiplier
        WHERE leg IN ('spot_compensation', 'perp_compensation')
          AND compensation_target_base_quantity IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_order_leg_compensation_tolerance_non_negative",
        "order_legs",
        type_="check",
    )
    op.drop_column("order_legs", "compensation_tolerance_base")
    op.drop_column(
        "order_legs",
        "compensation_target_base_quantity",
    )
