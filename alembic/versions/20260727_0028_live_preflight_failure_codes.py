"""Persist safe live preflight failure codes.

Revision ID: 20260727_0028
Revises: 20260727_0027
"""

from alembic import op

revision = "20260727_0028"
down_revision = "20260727_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        "failure_code IS NULL OR failure_code IN "
        "('market_data_expired', 'no_fills', "
        "'exposure_neutralized', 'state_transition_failed', "
        "'credential_missing', 'account_client_failed', "
        "'account_snapshot_failed', 'remote_state_failed', "
        "'account_snapshot_stale', 'trade_permission_unconfirmed', "
        "'position_mode_unknown', 'remote_state_incomplete', "
        "'remote_open_orders', 'intent_missing', "
        "'intent_legs_invalid', 'remote_positions_present', "
        "'balance_insufficient', 'perp_configuration_failed', "
        "'close_state_mismatch', 'preflight_internal_error')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_intent_failure_code",
        "trade_intents",
        "failure_code IS NULL OR failure_code IN "
        "('market_data_expired', 'no_fills', "
        "'exposure_neutralized', 'state_transition_failed')",
    )
