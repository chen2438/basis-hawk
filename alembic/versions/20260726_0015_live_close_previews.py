"""Allow persisted previews to describe live closes.

Revision ID: 20260726_0015
Revises: 20260726_0014
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0015"
down_revision = "20260726_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_previews",
        sa.Column(
            "action",
            sa.String(length=20),
            server_default="open",
            nullable=False,
        ),
    )
    op.add_column(
        "trade_previews",
        sa.Column("paired_position_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        op.f("ix_trade_previews_paired_position_id"),
        "trade_previews",
        ["paired_position_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_trade_previews_paired_position_id",
        "trade_previews",
        "paired_positions",
        ["paired_position_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_trade_preview_action",
        "trade_previews",
        "action IN ('open', 'close')",
    )
    op.create_check_constraint(
        "ck_trade_preview_position_action",
        "trade_previews",
        "(action = 'open' AND paired_position_id IS NULL) OR "
        "(action = 'close' AND paired_position_id IS NOT NULL)",
    )
    op.alter_column(
        "trade_previews",
        "action",
        existing_type=sa.String(length=20),
        server_default=None,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_preview_position_action",
        "trade_previews",
        type_="check",
    )
    op.drop_constraint(
        "ck_trade_preview_action",
        "trade_previews",
        type_="check",
    )
    op.drop_constraint(
        "fk_trade_previews_paired_position_id",
        "trade_previews",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_trade_previews_paired_position_id"),
        table_name="trade_previews",
    )
    op.drop_column("trade_previews", "paired_position_id")
    op.drop_column("trade_previews", "action")
