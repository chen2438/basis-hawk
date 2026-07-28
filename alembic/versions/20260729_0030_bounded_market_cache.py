"""Bundle the latest market cache and downsample retained trend history.

Revision ID: 20260729_0030
Revises: 20260728_0029
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_0030"
down_revision = "20260728_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "latest_opportunities_bundled",
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("exchange"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            INSERT INTO latest_opportunities_bundled (
                exchange, observed_at, payload, updated_at
            )
            SELECT
                exchange,
                max(observed_at),
                json_agg(payload::json ORDER BY base_asset)::text,
                max(updated_at)
            FROM latest_opportunities
            GROUP BY exchange
            """
        )
    op.drop_table("latest_opportunities")
    op.rename_table(
        "latest_opportunities_bundled",
        "latest_opportunities",
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE latest_opportunities SET (
            fillfactor = 50,
            toast_tuple_target = 128,
            autovacuum_vacuum_scale_factor = 0,
            autovacuum_vacuum_threshold = 5,
            autovacuum_analyze_scale_factor = 0,
            autovacuum_analyze_threshold = 5,
            toast.autovacuum_vacuum_scale_factor = 0,
            toast.autovacuum_vacuum_threshold = 5
        )
        """
    )
    op.execute(
        """
        DELETE FROM opportunity_snapshots AS target
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY
                            exchange,
                            base_asset,
                            date_trunc('hour', observed_at)
                        ORDER BY observed_at DESC, id DESC
                    ) AS position
                FROM opportunity_snapshots
            ) AS ranked
            WHERE position > 1
        ) AS redundant
        WHERE target.id = redundant.id
        """
    )
    with op.get_context().autocommit_block():
        op.execute("VACUUM (FULL, ANALYZE) opportunity_snapshots")


def downgrade() -> None:
    op.create_table(
        "latest_opportunities_expanded",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("base_asset", sa.String(length=40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            INSERT INTO latest_opportunities_expanded (
                key, exchange, base_asset, observed_at, payload, updated_at
            )
            SELECT
                exchange || ':' || item->>'base_asset',
                exchange,
                item->>'base_asset',
                (item->>'observed_at')::timestamptz,
                item::text,
                updated_at
            FROM latest_opportunities
            CROSS JOIN LATERAL json_array_elements(payload::json) AS item
            """
        )
    op.drop_table("latest_opportunities")
    op.rename_table(
        "latest_opportunities_expanded",
        "latest_opportunities",
    )
    op.create_index(
        op.f("ix_latest_opportunities_exchange"),
        "latest_opportunities",
        ["exchange"],
        unique=False,
    )
