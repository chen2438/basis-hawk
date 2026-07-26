"""Persist private event stream health.

Revision ID: 20260726_0013
Revises: 20260726_0012
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0013"
down_revision = "20260726_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "private_stream_states",
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("connected", sa.Boolean(), nullable=False),
        sa.Column("authenticated", sa.Boolean(), nullable=False),
        sa.Column("orders_subscribed", sa.Boolean(), nullable=False),
        sa.Column("fills_subscribed", sa.Boolean(), nullable=False),
        sa.Column("positions_subscribed", sa.Boolean(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("exchange", "environment"),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "private_stream_ready",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("account_reconciliation", "private_stream_ready")
    op.drop_table("private_stream_states")
