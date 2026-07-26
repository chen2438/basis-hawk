"""Track notification source transitions.

Revision ID: 20260726_0021
Revises: 20260726_0020
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0021"
down_revision = "20260726_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_projection_state",
        sa.Column("source_key", sa.String(length=150), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_notification_projection_generation",
        ),
        sa.PrimaryKeyConstraint("source_key"),
    )


def downgrade() -> None:
    op.drop_table("notification_projection_state")
