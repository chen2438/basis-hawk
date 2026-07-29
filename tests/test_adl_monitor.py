from datetime import UTC, datetime

from basis_hawk.accounts import RemoteAdlBatch, RemoteAdlPosition
from basis_hawk.adl import AdlMonitorService
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.storage import Database


async def test_adl_monitor_persists_live_ranks_and_event_only_accounts() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    binance = await credentials.create_account(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="main",
        secrets=ExchangeSecrets(
            api_key="binance-key",
            api_secret="binance-secret",
        ),
        actor="admin",
    )
    mexc = await credentials.create_account(
        exchange=Exchange.MEXC,
        environment=ExchangeEnvironment.LIVE,
        label="event stream",
        secrets=ExchangeSecrets(
            api_key="mexc-api-key",
            api_secret="mexc-api-secret",
        ),
        actor="admin",
    )

    class Client:
        def __init__(self, exchange: Exchange) -> None:
            self.exchange = exchange
            self.closed = False

        async def adl_ranks(self) -> RemoteAdlBatch:
            if self.exchange == Exchange.MEXC:
                return RemoteAdlBatch(
                    positions=[],
                    complete=False,
                    event_only=True,
                    incomplete_reason="event only",
                )
            return RemoteAdlBatch(
                positions=[
                    RemoteAdlPosition(
                        symbol="BTCUSDT",
                        position_side="short",
                        native_value="3",
                        risk_level=4,
                        observed_at=datetime.now(UTC),
                    )
                ],
                complete=True,
            )

        async def close(self) -> None:
            self.closed = True

    service = AdlMonitorService(
        database,
        credentials,
        lambda exchange, secrets, environment: Client(exchange),
    )
    items = await service.refresh()
    by_account = {item.account_id: item for item in items}
    assert by_account[binance.id].risk_level == 4
    assert by_account[binance.id].position_side == "short"
    assert by_account[mexc.id].event_only is True
    assert by_account[mexc.id].risk_level is None
    assert by_account[mexc.id].symbol == "*"
    await database.close()
