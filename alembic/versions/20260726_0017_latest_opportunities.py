"""Persist the latest opportunity per exchange symbol for the worker.

Revision ID: 20260726_0017
Revises: 20260726_0016
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0017"
down_revision = "20260726_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "latest_opportunities",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        op.f("ix_latest_opportunities_exchange"),
        "latest_opportunities",
        ["exchange"],
        unique=False,
    )
    op.create_index(
        op.f("ix_latest_opportunities_observed_at"),
        "latest_opportunities",
        ["observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_latest_opportunities_observed_at"),
        table_name="latest_opportunities",
    )
    op.drop_index(
        op.f("ix_latest_opportunities_exchange"),
        table_name="latest_opportunities",
    )
    op.drop_table("latest_opportunities")
