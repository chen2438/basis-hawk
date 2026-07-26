from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.private_stream_factory import create_private_stream_connections
from basis_hawk.storage import Database


async def test_factory_builds_only_implemented_private_stream_connections() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    for exchange, passphrase in (
        (Exchange.BINANCE, None),
        (Exchange.OKX, "test-passphrase"),
    ):
        await credentials.save(
            exchange=exchange,
            environment=ExchangeEnvironment.LIVE,
            label="primary",
            secrets=ExchangeSecrets(
                api_key=f"{exchange.value}-api-key",
                api_secret=f"{exchange.value}-api-secret",
                passphrase=passphrase,
            ),
            actor="test",
        )

    connections = await create_private_stream_connections(
        credentials,
        timeout_seconds=7,
    )

    assert len(connections) == 1
    assert connections[0].exchange == Exchange.BINANCE
    assert connections[0].environment == ExchangeEnvironment.LIVE
    assert connections[0].timeout_seconds == 7
    await database.close()
