"""Plan opening hedge size after confirmed spot base-asset fees.

Revision ID: 20260729_0033
Revises: 20260729_0032
"""

import sqlalchemy as sa

from alembic import op

revision = "20260729_0033"
down_revision = "20260729_0032"
branch_labels = None
depends_on = None


FAILURE_CODES = (
    "failure_code IS NULL OR failure_code IN "
    "('market_data_expired', 'market_unexecutable', 'no_fills', "
    "'exposure_neutralized', 'state_transition_failed', "
    "'credential_missing', 'account_client_failed', "
    "'account_snapshot_failed', 'remote_state_failed', "
    "'account_snapshot_stale', 'trade_permission_unconfirmed', "
    "'position_mode_unknown', 'remote_state_incomplete', "
    "'remote_open_orders', 'intent_missing', "
    "'intent_legs_invalid', 'remote_positions_present', "
    "'balance_insufficient', 'perp_configuration_failed', "
    "'close_state_mismatch', 'spot_fee_mode_changed', "
    "'preflight_internal_error')"
)

PREVIOUS_FAILURE_CODES = FAILURE_CODES.replace(
    "'close_state_mismatch', 'spot_fee_mode_changed', ",
    "'close_state_mismatch', ",
)


def upgrade() -> None:
    op.add_column(
        "account_snapshots",
        sa.Column("spot_buy_fee_in_base", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "trade_intents",
        sa.Column(
            "spot_buy_fee_in_base",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "trade_intents",
        "spot_buy_fee_in_base",
        server_default=None,
    )
    op.drop_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        FAILURE_CODES,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        type_="check",
    )
    op.execute(
        "UPDATE trade_intents SET failure_code = NULL "
        "WHERE failure_code = 'spot_fee_mode_changed'"
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        PREVIOUS_FAILURE_CODES,
    )
    op.drop_column("trade_intents", "spot_buy_fee_in_base")
    op.drop_column("account_snapshots", "spot_buy_fee_in_base")
