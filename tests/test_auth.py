from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
from cryptography.exceptions import InvalidTag

from basis_hawk.api import CSRF_COOKIE, create_app
from basis_hawk.auth import AuthService, LoginAttemptLimiter
from basis_hawk.crypto import EncryptedPayload, SecretCipher
from basis_hawk.service import ScannerService
from basis_hawk.storage import Database


def test_secret_cipher_round_trip_and_context_binding() -> None:
    cipher = SecretCipher(SecretCipher.generate_key())
    encrypted = cipher.encrypt_json(
        {"api_key": "key", "api_secret": "secret"},
        associated_data="credential:one",
    )
    assert cipher.decrypt_json(encrypted, associated_data="credential:one") == {
        "api_key": "key",
        "api_secret": "secret",
    }
    with pytest.raises(InvalidTag):
        cipher.decrypt_json(encrypted, associated_data="credential:two")


def test_login_attempt_limiter_blocks_repeated_failures() -> None:
    limiter = LoginAttemptLimiter(maximum_attempts=2)
    assert limiter.allowed("address:admin")
    limiter.record_failure("address:admin")
    assert limiter.allowed("address:admin")
    limiter.record_failure("address:admin")
    assert not limiter.allowed("address:admin")
    limiter.clear("address:admin")
    assert limiter.allowed("address:admin")


async def test_login_session_and_csrf_protection() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    cipher = SecretCipher(SecretCipher.generate_key())
    auth = AuthService(database, cipher)
    uri = await auth.bootstrap_admin("admin", "correct horse battery staple")
    secret = parse_qs(urlparse(uri).query)["secret"][0]
    service = ScannerService(database, {})
    await service.initialize()
    app = create_app(
        service,
        manage_lifecycle=False,
        auth_required=True,
        auth_service=auth,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    ) as client:
        assert (await client.get("/api/settings")).status_code == 401
        login = await client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
                "totp_code": pyotp.TOTP(secret).now(),
            },
        )
        assert login.status_code == 200
        assert (await client.get("/api/auth/session")).json() == {"username": "admin"}
        settings = (await client.get("/api/settings")).json()
        assert (await client.put("/api/settings", json=settings)).status_code == 403
        csrf = client.cookies.get(CSRF_COOKIE)
        assert (
            await client.put(
                "/api/settings",
                json=settings,
                headers={"X-CSRF-Token": csrf},
            )
        ).status_code == 200
    await database.close()


async def test_encrypted_exchange_credential_storage() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    cipher = SecretCipher(SecretCipher.generate_key())
    encrypted = cipher.encrypt_json(
        {"api_key": "abcd1234", "api_secret": "do-not-store-plain"},
        associated_data="binance:live",
    )
    await database.save_exchange_credential(
        exchange="binance",
        environment="live",
        label="primary",
        masked_api_key="abcd…1234",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        key_version=encrypted.key_version,
    )
    row = await database.exchange_credential("binance", "live")
    assert row is not None
    assert "do-not-store-plain" not in row.ciphertext
    assert cipher.decrypt_json(
        EncryptedPayload(
            ciphertext=row.ciphertext,
            nonce=row.nonce,
            key_version=row.key_version,
        ),
        associated_data="binance:live",
    )["api_secret"] == "do-not-store-plain"
    await database.close()
