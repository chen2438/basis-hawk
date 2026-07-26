"""Persist remote open orders and positions.

Revision ID: 20260726_0003
Revises: 20260726_0002
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0003"
down_revision = "20260726_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "trading_state_complete",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "open_order_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "position_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "remote_open_order_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=100), nullable=False),
        sa.Column("client_order_id", sa.String(length=100), nullable=True),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=100), nullable=False),
        sa.Column("side", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "original_quantity",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "filled_quantity",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["account_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remote_open_order_snapshots_account_snapshot_id",
        "remote_open_order_snapshots",
        ["account_snapshot_id"],
    )
    op.create_table(
        "remote_position_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=100), nullable=False),
        sa.Column("side", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("mark_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "liquidation_price",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("leverage", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("isolated", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["account_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remote_position_snapshots_account_snapshot_id",
        "remote_position_snapshots",
        ["account_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remote_position_snapshots_account_snapshot_id",
        table_name="remote_position_snapshots",
    )
    op.drop_table("remote_position_snapshots")
    op.drop_index(
        "ix_remote_open_order_snapshots_account_snapshot_id",
        table_name="remote_open_order_snapshots",
    )
    op.drop_table("remote_open_order_snapshots")
    op.drop_column("account_reconciliation", "position_count")
    op.drop_column("account_reconciliation", "open_order_count")
    op.drop_column("account_reconciliation", "trading_state_complete")
