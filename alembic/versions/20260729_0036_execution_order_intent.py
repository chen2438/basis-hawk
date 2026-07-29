"""Persist the executable intent on every multi-leg order.

Revision ID: 20260729_0036
Revises: 20260729_0035
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_0036"
down_revision = "20260729_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_orders",
        sa.Column("side", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("reduce_only", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("purpose", sa.String(length=20), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE execution_orders AS target
            SET side = leg.side,
                reduce_only = leg.reduce_only,
                purpose = 'primary'
            FROM execution_task_legs AS leg
            WHERE target.task_leg_id = leg.id
            """
        )
    )
    op.alter_column(
        "execution_orders",
        "side",
        existing_type=sa.String(length=10),
        nullable=False,
    )
    op.alter_column(
        "execution_orders",
        "reduce_only",
        existing_type=sa.Boolean(),
        nullable=False,
    )
    op.alter_column(
        "execution_orders",
        "purpose",
        existing_type=sa.String(length=20),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_execution_order_side",
        "execution_orders",
        "side IN ('buy', 'sell')",
    )
    op.create_check_constraint(
        "ck_execution_order_purpose",
        "execution_orders",
        "purpose IN ('primary', 'compensation')",
    )
    op.create_index(
        "ix_execution_orders_purpose",
        "execution_orders",
        ["purpose"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_orders_purpose",
        table_name="execution_orders",
    )
    op.drop_constraint(
        "ck_execution_order_purpose",
        "execution_orders",
        type_="check",
    )
    op.drop_constraint(
        "ck_execution_order_side",
        "execution_orders",
        type_="check",
    )
    op.drop_column("execution_orders", "purpose")
    op.drop_column("execution_orders", "reduce_only")
    op.drop_column("execution_orders", "side")
