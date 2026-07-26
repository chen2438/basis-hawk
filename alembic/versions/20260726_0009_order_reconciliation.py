"""Track ACK-lost order reconciliation.

Revision ID: 20260726_0009
Revises: 20260726_0008
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0009"
down_revision = "20260726_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "order_reconciliation_complete",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "account_reconciliation",
        sa.Column(
            "recovered_order_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.alter_column(
        "account_reconciliation",
        "order_reconciliation_complete",
        server_default=None,
    )
    op.alter_column(
        "account_reconciliation",
        "recovered_order_count",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("account_reconciliation", "recovered_order_count")
    op.drop_column(
        "account_reconciliation",
        "order_reconciliation_complete",
    )
