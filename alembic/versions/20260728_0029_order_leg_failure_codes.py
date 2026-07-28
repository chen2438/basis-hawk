"""Persist safe order-leg rejection codes.

Revision ID: 20260728_0029
Revises: 20260727_0028
"""

import sqlalchemy as sa

from alembic import op

revision = "20260728_0029"
down_revision = "20260727_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_legs",
        sa.Column("failure_code", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_legs", "failure_code")
