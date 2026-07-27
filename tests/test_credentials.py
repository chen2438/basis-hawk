from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from basis_hawk.accounts import AccountSnapshot, PositionMode
from basis_hawk.api import create_app
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


async def test_credential_service_requires_exchange_passphrase() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    service = CredentialService(database, SecretCipher(SecretCipher.generate_key()))
    with pytest.raises(ValueError, match="passphrase"):
        await service.save(
            exchange=Exchange.BITGET,
            environment=ExchangeEnvironment.LIVE,
            label="main",
            secrets=ExchangeSecrets(
                api_key="api-key-value",
                api_secret="api-secret-value",
            ),
            actor="admin",
        )
    await database.close()


async def test_credential_api_never_echoes_plaintext() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    scanner = ScannerService(database, {})
    await scanner.initialize()
    credentials = CredentialService(database, SecretCipher(SecretCipher.generate_key()))
    received_secrets: list[ExchangeSecrets] = []

    class FakeAccountClient:
        async def snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExchangeEnvironment.LIVE,
                observed_at=datetime.now(UTC),
                spot_usdt_available=Decimal("10"),
                perp_usdt_available=Decimal("8"),
                perp_usdt_equity=Decimal("9"),
                shared_balance=False,
                account_mode="spot+usdt_futures",
                position_mode=PositionMode.ONE_WAY,
                trade_permission=True,
            )

        async def close(self) -> None:
            return None

    def account_factory(exchange, secrets, environment):
        assert exchange == Exchange.BINANCE
        assert environment == ExchangeEnvironment.LIVE
        received_secrets.append(secrets)
        return FakeAccountClient()

    app = create_app(
        scanner,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
        account_client_factory=account_factory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/accounts/binance/live/credentials",
            json={
                "label": "primary",
                "api_key": "abcd1234-api-key",
                "api_secret": "super-secret-value",
            },
        )
        assert response.status_code == 200
        assert response.json()["masked_api_key"] == "abcd…-key"
        assert "abcd1234-api-key" not in response.text
        assert "super-secret-value" not in response.text

        summaries = await client.get("/api/accounts/credentials")
        assert summaries.json()["items"][0]["label"] == "primary"
        assert "super-secret-value" not in summaries.text

        snapshot = await client.get("/api/accounts/binance/live/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["spot_usdt_available"] == "10"
        assert snapshot.json()["position_mode"] == "one_way"
        assert received_secrets == [
            ExchangeSecrets(
                api_key="abcd1234-api-key",
                api_secret="super-secret-value",
            )
        ]

        loaded = await credentials.load(Exchange.BINANCE, ExchangeEnvironment.LIVE)
        assert loaded == ExchangeSecrets(
            api_key="abcd1234-api-key",
            api_secret="super-secret-value",
        )
        row = await database.exchange_credential("binance", "live")
        assert row is not None
        assert "super-secret-value" not in row.ciphertext

        deleted = await client.delete("/api/accounts/binance/live/credentials")
        assert deleted.status_code == 204
        assert await credentials.load(Exchange.BINANCE, ExchangeEnvironment.LIVE) is None
    await database.close()


async def test_bybit_position_mode_declaration_can_be_updated_without_new_keys() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    scanner = ScannerService(database, {})
    await scanner.initialize()
    credentials = CredentialService(database, SecretCipher(SecretCipher.generate_key()))
    app = create_app(
        scanner,
        manage_lifecycle=False,
        auth_required=False,
        credential_service=credentials,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        saved = await client.put(
            "/api/accounts/bybit/live/credentials",
            json={
                "label": "primary",
                "api_key": "bybit-api-key",
                "api_secret": "bybit-api-secret",
                "position_mode": "one_way",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["position_mode"] == "one_way"

        updated = await client.put(
            "/api/accounts/bybit/live/position-mode",
            json={"position_mode": "hedge", "confirmed": True},
        )
        assert updated.status_code == 200
        assert updated.json()["position_mode"] == "hedge"
        loaded = await credentials.load(Exchange.BYBIT, ExchangeEnvironment.LIVE)
        assert loaded == ExchangeSecrets(
            api_key="bybit-api-key",
            api_secret="bybit-api-secret",
            position_mode="hedge",
        )
    await database.close()
