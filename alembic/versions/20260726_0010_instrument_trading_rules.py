"""Persist instrument trading rules.

Revision ID: 20260726_0010
Revises: 20260726_0009
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20260726_0010"
down_revision = "20260726_0009"
branch_labels = None
depends_on = None

_COLUMNS = (
    "spot_price_increment",
    "spot_quantity_increment",
    "spot_min_quantity",
    "spot_min_notional",
    "perp_price_increment",
    "perp_quantity_increment",
    "perp_min_quantity",
    "perp_min_notional",
    "perp_contract_size",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "instruments",
            sa.Column(
                name,
                sa.Numeric(precision=38, scale=18),
                server_default="0",
                nullable=False,
            ),
        )
        op.alter_column("instruments", name, server_default=None)


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("instruments", name)
