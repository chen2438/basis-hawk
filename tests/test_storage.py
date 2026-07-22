from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.models import Exchange, FundingObservation, ScannerSettings
from basis_hawk.storage import Database


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
