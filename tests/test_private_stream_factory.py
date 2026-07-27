from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.private_stream_factory import create_private_stream_connections
from basis_hawk.storage import Database


async def test_factory_builds_implemented_private_stream_connections() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = CredentialService(
        database,
        SecretCipher(SecretCipher.generate_key()),
    )
    for exchange, passphrase in (
        (Exchange.BINANCE, None),
        (Exchange.OKX, "test-passphrase"),
        (Exchange.BYBIT, None),
        (Exchange.BITGET, "test-passphrase"),
        (Exchange.GATE, None),
        (Exchange.MEXC, None),
    ):
        await credentials.save(
            exchange=exchange,
            environment=ExchangeEnvironment.LIVE,
            label="primary",
            secrets=ExchangeSecrets(
                api_key=f"{exchange.value}-api-key",
                api_secret=f"{exchange.value}-api-secret",
                passphrase=passphrase,
                position_mode="one_way" if exchange == Exchange.BYBIT else None,
            ),
            actor="test",
        )
    await credentials.save(
        exchange=Exchange.GATE,
        environment=ExchangeEnvironment.SANDBOX,
        label="sandbox",
        secrets=ExchangeSecrets(
            api_key="gate-sandbox-api-key",
            api_secret="gate-sandbox-api-secret",
        ),
        actor="test",
    )

    connections = await create_private_stream_connections(
        credentials,
        timeout_seconds=7,
    )

    assert [item.exchange for item in connections] == [
        Exchange.BINANCE,
        Exchange.BITGET,
        Exchange.BYBIT,
        Exchange.GATE,
        Exchange.GATE,
        Exchange.MEXC,
        Exchange.OKX,
    ]
    assert [
        item.environment
        for item in connections
        if item.exchange == Exchange.GATE
    ] == [
        ExchangeEnvironment.LIVE,
        ExchangeEnvironment.SANDBOX,
    ]
    assert all(
        item.environment == ExchangeEnvironment.LIVE
        for item in connections
        if item.exchange != Exchange.GATE
    )
    assert all(item.timeout_seconds == 7 for item in connections)
    await database.close()
