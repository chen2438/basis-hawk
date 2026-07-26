"""Create VPS production foundation.

Revision ID: 20260726_0001
Revises:
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_ciphertext", sa.Text(), nullable=False),
        sa.Column("totp_nonce", sa.String(length=80), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_table(
        "exchange_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("masked_api_key", sa.String(length=100), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=80), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange", "environment", name="uq_exchange_credential"),
    )
    op.create_table(
        "funding_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("funding_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate", sa.String(length=48), nullable=False),
        sa.Column("interval_hours", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_funding",
        "funding_observations",
        ["exchange", "base_asset", "funding_at"],
        unique=True,
    )
    op.create_table(
        "instruments",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("spot_symbol", sa.String(length=80), nullable=False),
        sa.Column("perp_symbol", sa.String(length=80), nullable=False),
        sa.Column("interval_hours", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_instruments_exchange", "instruments", ["exchange"])
    op.create_table(
        "opportunity_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_snapshot_history",
        "opportunity_snapshots",
        ["exchange", "base_asset", "observed_at"],
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admin_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_table("settings")
    op.drop_index("ix_snapshot_history", table_name="opportunity_snapshots")
    op.drop_table("opportunity_snapshots")
    op.drop_index("ix_instruments_exchange", table_name="instruments")
    op.drop_table("instruments")
    op.drop_index("uq_funding", table_name="funding_observations")
    op.drop_table("funding_observations")
    op.drop_table("exchange_credentials")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("admin_users")
