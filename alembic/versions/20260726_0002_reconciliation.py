"""Add account reconciliation safety state.

Revision ID: 20260726_0002
Revises: 20260726_0001
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0002"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spot_usdt_available", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("perp_usdt_available", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("perp_usdt_equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("shared_balance", sa.Boolean(), nullable=False),
        sa.Column("account_mode", sa.String(length=100), nullable=False),
        sa.Column("position_mode", sa.String(length=20), nullable=False),
        sa.Column("trade_permission", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_snapshots_observed_at",
        "account_snapshots",
        ["observed_at"],
    )
    op.create_index(
        "ix_account_snapshot_history",
        "account_snapshots",
        ["exchange", "environment", "observed_at"],
    )
    op.create_table(
        "account_reconciliation",
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["account_snapshots.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("exchange", "environment"),
    )
    op.create_table(
        "execution_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("execution_control")
    op.drop_table("account_reconciliation")
    op.drop_index("ix_account_snapshot_history", table_name="account_snapshots")
    op.drop_index("ix_account_snapshots_observed_at", table_name="account_snapshots")
    op.drop_table("account_snapshots")
