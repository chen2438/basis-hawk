"""Persist actual funding income records.

Revision ID: 20260727_0023
Revises: 20260726_0022
Create Date: 2026-07-27
"""

import sqlalchemy as sa

from alembic import op

revision = "20260727_0023"
down_revision = "20260726_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "funding_income_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "funding_income_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "funding_income",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("exchange_record_id", sa.String(length=120), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=100), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("rate", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column(
            "position_value",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("asset = 'USDT'", name="ck_funding_income_usdt_only"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange",
            "environment",
            "exchange_record_id",
            name="uq_funding_income_remote_record",
        ),
    )
    op.create_index(
        "ix_funding_income_occurred_at",
        "funding_income",
        ["occurred_at"],
    )
    op.create_index(
        "ix_funding_income_account",
        "funding_income",
        ["exchange", "environment", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_funding_income_account", table_name="funding_income")
    op.drop_index("ix_funding_income_occurred_at", table_name="funding_income")
    op.drop_table("funding_income")
    op.drop_column("account_reconciliation", "funding_income_count")
    op.drop_column("account_reconciliation", "funding_income_complete")
