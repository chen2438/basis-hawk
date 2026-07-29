"""Persist an expected market-unexecutable trade outcome.

Revision ID: 20260729_0032
Revises: 20260729_0031
"""

from alembic import op

revision = "20260729_0032"
down_revision = "20260729_0031"
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
    "'close_state_mismatch', 'preflight_internal_error')"
)

PREVIOUS_FAILURE_CODES = FAILURE_CODES.replace(
    "'market_data_expired', 'market_unexecutable', ",
    "'market_data_expired', ",
)


def upgrade() -> None:
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
        "WHERE failure_code = 'market_unexecutable'"
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        PREVIOUS_FAILURE_CODES,
    )
