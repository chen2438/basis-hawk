"""Add bounded internal transfer ledger.

Revision ID: 20260726_0022
Revises: 20260726_0021
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0022"
down_revision = "20260726_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "internal_transfers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("asset", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("exchange_transfer_id", sa.String(length=100), nullable=True),
        sa.Column(
            "source_balance_before",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column(
            "target_balance_before",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column(
            "expected_target_balance",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_internal_transfer_amount_positive",
        ),
        sa.CheckConstraint(
            "direction IN ('spot_to_perp', 'perp_to_spot')",
            name="ck_internal_transfer_direction",
        ),
        sa.CheckConstraint(
            "status IN "
            "('planned', 'submitted', 'pending', 'completed', 'failed', "
            "'manual_review')",
            name="ck_internal_transfer_status",
        ),
        sa.CheckConstraint(
            "asset = 'USDT'",
            name="ck_internal_transfer_usdt_only",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_internal_transfers_exchange",
        "internal_transfers",
        ["exchange"],
    )
    op.create_index(
        "ix_internal_transfers_status",
        "internal_transfers",
        ["status"],
    )
    op.create_index(
        "ix_internal_transfers_created_at",
        "internal_transfers",
        ["created_at"],
    )
    op.create_index(
        "ix_internal_transfer_daily_limit",
        "internal_transfers",
        ["created_at", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_internal_transfer_daily_limit",
        table_name="internal_transfers",
    )
    op.drop_index(
        "ix_internal_transfers_created_at",
        table_name="internal_transfers",
    )
    op.drop_index(
        "ix_internal_transfers_status",
        table_name="internal_transfers",
    )
    op.drop_index(
        "ix_internal_transfers_exchange",
        table_name="internal_transfers",
    )
    op.drop_table("internal_transfers")
