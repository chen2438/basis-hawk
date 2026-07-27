"""Persist manual trade preview price bounds.

Revision ID: 20260727_0025
Revises: 20260727_0024
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260727_0025"
down_revision = "20260727_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_previews",
        sa.Column("spot_limit_price", sa.Numeric(38, 18), nullable=True),
    )
    op.add_column(
        "trade_previews",
        sa.Column("perp_limit_price", sa.Numeric(38, 18), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_previews", "perp_limit_price")
    op.drop_column("trade_previews", "spot_limit_price")
