from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from basis_hawk.models import Exchange, FundingObservation, ScannerSettings
from basis_hawk.storage import Database


async def test_default_settings_scan_up_to_500_symbols() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    assert (await database.load_settings()).universe_size == 500
    await database.close()


def test_settings_reject_more_than_500_symbols() -> None:
    with pytest.raises(ValidationError):
        ScannerSettings(universe_size=501)


def test_settings_fill_fee_defaults_for_new_exchanges() -> None:
    settings = ScannerSettings.model_validate(
        {
            "fees": {
                "binance": {
                    "spot_taker": "0.002",
                    "perp_taker": "0.0005",
                }
            }
        }
    )
    assert settings.fees[Exchange.BINANCE].spot_taker == Decimal("0.002")
    assert Exchange.BITGET in settings.fees
    assert Exchange.GATE in settings.fees


async def test_settings_and_funding_round_trip() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    settings = ScannerSettings(universe_size=42)
    await database.save_settings(settings)
    assert (await database.load_settings()).universe_size == 42
    item = FundingObservation(
        exchange=Exchange.OKX,
        base_asset="BTC",
        rate=Decimal("0.0001"),
        funding_at=datetime(2026, 7, 23, tzinfo=UTC),
        settled=True,
    )
    await database.save_funding([item, item])
    history = await database.funding_history("okx", "BTC", since=datetime(2026, 1, 1, tzinfo=UTC))
    assert [row.rate for row in history] == [Decimal("0.0001")]
    await database.close()
