from datetime import UTC, datetime
from decimal import Decimal

import httpx

from basis_hawk.api import create_app
from basis_hawk.credentials import (
    AccountFeeSchedule,
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange, Opportunity, Quality
from basis_hawk.opportunities import OpportunityService, OpportunityType
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


def _opportunity(
    exchange: Exchange,
    *,
    spot_ask: str,
    perp_bid: str,
    perp_ask: str,
    funding_rate: str,
    interval_hours: str = "8",
) -> Opportunity:
    now = datetime.now(UTC)
    return Opportunity(
        exchange=exchange,
        base_asset="BTC",
        spot_symbol="BTCUSDT",
        perp_symbol=(
            "BTC-USDT-SWAP"
            if exchange == Exchange.OKX
            else "BTCUSDT"
        ),
        observed_at=now,
        spot_bid=Decimal(spot_ask) - Decimal("1"),
        spot_ask=Decimal(spot_ask),
        perp_bid=Decimal(perp_bid),
        perp_ask=Decimal(perp_ask),
        executable_basis=Decimal("0"),
        top_book_notional=Decimal("10000"),
        close_top_book_notional=Decimal("8000"),
        current_funding_rate=Decimal(funding_rate),
        funding_interval_hours=Decimal(interval_hours),
        next_funding_at=now,
        current_apr=(
            Decimal(funding_rate)
            * Decimal("24")
            / Decimal(interval_hours)
            * Decimal("365")
        ),
        apr_24h=None,
        apr_7d=None,
        net_return=None,
        spot_quote_volume_24h=Decimal("10000000"),
        perp_quote_volume_24h=Decimal("20000000"),
        spot_taker_fee=Decimal("0.001"),
        perp_taker_fee=Decimal("0.0005"),
        quality=Quality.HEALTHY,
        spot_ask_notional=Decimal("9000"),
        perp_bid_notional=Decimal("10000"),
    )


async def test_opportunity_service_builds_all_three_cross_venue_views() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    await database.save_latest_opportunities(
        [
            _opportunity(
                Exchange.BINANCE,
                spot_ask="100",
                perp_bid="104",
                perp_ask="105",
                funding_rate="0.001",
            ),
            _opportunity(
                Exchange.OKX,
                spot_ask="99",
                perp_bid="101",
                perp_ask="102",
                funding_rate="0.0002",
                interval_hours="4",
            ),
        ]
    )
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    binance_account = await credentials.create_account(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="main",
        secrets=ExchangeSecrets(
            api_key="api-key-123",
            api_secret="api-secret-123",
        ),
        actor="admin",
        fees=AccountFeeSchedule(
            spot_taker=Decimal("0.0001"),
            perpetual_taker=Decimal("0.0002"),
            source="manual",
        ),
    )
    service = OpportunityService(database)

    funding = await service.list(
        opportunity_type=OpportunityType.FUNDING,
        holding_days=7,
    )
    assert len(funding) == 4
    best_funding = funding[0]
    assert [
        (item.exchange, item.market_type, item.side)
        for item in best_funding.legs
    ] == [
        ("okx", "spot", "buy"),
        ("binance", "perpetual", "sell"),
    ]
    assert best_funding.entry_spread == Decimal("104") / Decimal("99") - 1
    assert best_funding.executable_notional_usdt == Decimal("9000")
    assert best_funding.legs[1].account_id == binance_account.id
    assert best_funding.legs[1].fee_rate == Decimal("0.0002")
    assert best_funding.legs[1].fee_source == "manual"

    cross = await service.list(
        opportunity_type=OpportunityType.CROSS_FUNDING,
        holding_days=7,
    )
    assert len(cross) == 1
    assert [
        (item.exchange, item.side)
        for item in cross[0].legs
    ] == [("binance", "sell"), ("okx", "buy")]
    assert cross[0].annualized_return == Decimal("0.657")

    basis = await service.list(
        opportunity_type=OpportunityType.BASIS,
        holding_days=7,
    )
    assert basis[0].projected_return >= basis[-1].projected_return
    assert basis[0].estimated_round_trip_fees == Decimal("0.0024")
    await database.close()


async def test_opportunity_api_filters_exchange_and_serializes_decimals() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    scanner = ScannerService(database, {})
    await scanner.initialize()
    await database.save_latest_opportunities(
        [
            _opportunity(
                Exchange.BINANCE,
                spot_ask="100",
                perp_bid="104",
                perp_ask="105",
                funding_rate="0.001",
            ),
            _opportunity(
                Exchange.OKX,
                spot_ask="99",
                perp_bid="101",
                perp_ask="102",
                funding_rate="0.0002",
            ),
        ]
    )
    app = create_app(
        scanner,
        manage_lifecycle=False,
        auth_required=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v2/opportunities",
            params={
                "type": "funding",
                "exchanges": "binance",
                "holding_days": 14,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["type"] == "funding"
        assert payload["holding_days"] == 14
        assert len(payload["items"]) == 1
        assert payload["items"][0]["annualized_return"] == "1.095"
        assert {
            item["exchange"] for item in payload["items"][0]["legs"]
        } == {"binance"}

        invalid = await client.get(
            "/api/v2/opportunities?exchanges=not-an-exchange"
        )
        assert invalid.status_code == 422
    await database.close()
