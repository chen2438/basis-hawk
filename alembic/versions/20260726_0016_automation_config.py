"""Persist immutable automation strategy versions and control state.

Revision ID: 20260726_0016
Revises: 20260726_0015
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0016"
down_revision = "20260726_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment IN ('sandbox', 'live')",
            name="ck_strategy_version_environment",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_table(
        "automation_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("active_strategy_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('disabled', 'enabled', 'paused')",
            name="ck_automation_control_state",
        ),
        sa.ForeignKeyConstraint(
            ["active_strategy_id"],
            ["strategy_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        """
        INSERT INTO automation_control
            (id, state, active_strategy_id, reason, updated_by, updated_at)
        VALUES
            (1, 'disabled', NULL, 'automatic trading is disabled by default',
             'system', CURRENT_TIMESTAMP)
        """
    )


def downgrade() -> None:
    op.drop_table("automation_control")
    op.drop_table("strategy_versions")
