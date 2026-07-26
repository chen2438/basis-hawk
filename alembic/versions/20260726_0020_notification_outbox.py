"""Add durable notification outbox.

Revision ID: 20260726_0020
Revises: 20260726_0019
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0020"
down_revision = "20260726_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_notification_outbox_attempts",
        ),
        sa.CheckConstraint(
            "channel IN ('telegram', 'email')",
            name="ck_notification_outbox_channel",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_notification_outbox_severity",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'retry', 'sent', 'dead')",
            name="ck_notification_outbox_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dedupe_key",
            "channel",
            name="uq_notification_outbox_dedupe_channel",
        ),
    )
    op.create_index(
        "ix_notification_outbox_event_type",
        "notification_outbox",
        ["event_type"],
    )
    op.create_index(
        "ix_notification_outbox_status",
        "notification_outbox",
        ["status"],
    )
    op.create_index(
        "ix_notification_outbox_next_attempt_at",
        "notification_outbox",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_claim",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_outbox_claim",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_next_attempt_at",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_status",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_event_type",
        table_name="notification_outbox",
    )
    op.drop_table("notification_outbox")
