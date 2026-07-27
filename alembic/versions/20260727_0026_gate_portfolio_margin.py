"""Persist perpetual margin mode for portfolio-margin reconciliation.

Revision ID: 20260727_0026
Revises: 20260727_0025
"""

import sqlalchemy as sa

from alembic import op

revision = "20260727_0026"
down_revision = "20260727_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_snapshots",
        sa.Column(
            "perp_margin_mode",
            sa.String(length=20),
            nullable=False,
            server_default="isolated",
        ),
    )
    op.alter_column(
        "account_snapshots",
        "perp_margin_mode",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("account_snapshots", "perp_margin_mode")
