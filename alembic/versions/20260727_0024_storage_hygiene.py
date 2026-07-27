"""Bound market history growth and compact the latest-opportunity table.

Revision ID: 20260727_0024
Revises: 20260727_0023
Create Date: 2026-07-27
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "20260727_0024"
down_revision = "20260727_0023"
branch_labels = None
depends_on = None


def _shorten_existing_retention() -> None:
    connection = op.get_bind()
    payload = connection.execute(
        sa.text("SELECT payload FROM settings WHERE key = 'scanner'")
    ).scalar_one_or_none()
    if payload is None:
        return
    settings = json.loads(payload)
    if int(settings.get("retention_days", 7)) <= 7:
        return
    settings["retention_days"] = 7
    connection.execute(
        sa.text("UPDATE settings SET payload = :payload WHERE key = 'scanner'"),
        {"payload": json.dumps(settings, separators=(",", ":"))},
    )


def upgrade() -> None:
    op.drop_index(
        op.f("ix_latest_opportunities_observed_at"),
        table_name="latest_opportunities",
    )
    _shorten_existing_retention()

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE latest_opportunities SET (
            fillfactor = 70,
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_vacuum_threshold = 50,
            autovacuum_analyze_scale_factor = 0.05,
            autovacuum_analyze_threshold = 50
        )
        """
    )
    with op.get_context().autocommit_block():
        op.execute("VACUUM (FULL, ANALYZE) latest_opportunities")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            ALTER TABLE latest_opportunities RESET (
                fillfactor,
                autovacuum_vacuum_scale_factor,
                autovacuum_vacuum_threshold,
                autovacuum_analyze_scale_factor,
                autovacuum_analyze_threshold
            )
            """
        )
    op.create_index(
        op.f("ix_latest_opportunities_observed_at"),
        "latest_opportunities",
        ["observed_at"],
        unique=False,
    )
