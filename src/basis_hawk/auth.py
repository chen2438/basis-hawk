from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.exceptions import InvalidTag

from basis_hawk.crypto import EncryptedPayload, SecretCipher
from basis_hawk.storage import AdminUserRow, Database

_password_hasher = PasswordHasher()


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class AuthenticatedSession:
    username: str
    session_token: str
    csrf_token: str
    expires_at: datetime


class AuthenticationError(RuntimeError):
    pass


class LoginAttemptLimiter:
    def __init__(self, *, maximum_attempts: int = 5, window_seconds: int = 900) -> None:
        self.maximum_attempts = maximum_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        values = self._attempts[key]
        cutoff = monotonic() - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()
        return len(values) < self.maximum_attempts

    def record_failure(self, key: str) -> None:
        self._attempts[key].append(monotonic())

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)


class AuthService:
    def __init__(
        self,
        database: Database,
        cipher: SecretCipher,
        *,
        session_hours: int = 12,
    ) -> None:
        self.database = database
        self.cipher = cipher
        self.session_hours = session_hours

    async def bootstrap_admin(self, username: str, password: str) -> str:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        if await self.database.admin_count():
            raise RuntimeError("an administrator already exists")
        secret = pyotp.random_base32()
        encrypted = self.cipher.encrypt(secret, associated_data=f"admin:{username}")
        await self.database.create_admin(
            username=username,
            password_hash=_password_hasher.hash(password),
            totp_ciphertext=encrypted.ciphertext,
            totp_nonce=encrypted.nonce,
            key_version=encrypted.key_version,
        )
        return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Basis Hawk")

    async def login(
        self,
        username: str,
        password: str,
        totp_code: str,
        *,
        remote_address: str | None = None,
    ) -> AuthenticatedSession:
        user = await self.database.get_admin_by_username(username.strip())
        if not user or not self._password_matches(user, password) or not self._totp_matches(
            user, totp_code
        ):
            await self.database.append_audit(
                "auth.login_failed",
                actor=username.strip() or "unknown",
                details={"remote_address": remote_address},
            )
            raise AuthenticationError("invalid username, password, or TOTP code")

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=self.session_hours)
        await self.database.create_session(
            admin_id=user.id,
            token_hash=_hash_token(session_token),
            csrf_hash=_hash_token(csrf_token),
            expires_at=expires_at,
        )
        await self.database.append_audit(
            "auth.login_succeeded",
            actor=user.username,
            details={"remote_address": remote_address},
        )
        return AuthenticatedSession(
            username=user.username,
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    async def authenticate(self, session_token: str | None) -> AdminUserRow | None:
        if not session_token:
            return None
        return await self.database.admin_for_session(
            token_hash=_hash_token(session_token),
            now=datetime.now(UTC),
        )

    async def validate_csrf(self, session_token: str, csrf_token: str | None) -> bool:
        if not csrf_token:
            return False
        expected = await self.database.csrf_hash_for_session(
            token_hash=_hash_token(session_token),
            now=datetime.now(UTC),
        )
        return bool(expected and hmac.compare_digest(expected, _hash_token(csrf_token)))

    async def logout(self, session_token: str | None, *, actor: str) -> None:
        if session_token:
            await self.database.delete_session(_hash_token(session_token))
        await self.database.append_audit("auth.logout", actor=actor, details={})

    def _password_matches(self, user: AdminUserRow, password: str) -> bool:
        try:
            return _password_hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def _totp_matches(self, user: AdminUserRow, code: str) -> bool:
        try:
            secret = self.cipher.decrypt(
                EncryptedPayload(
                    ciphertext=user.totp_ciphertext,
                    nonce=user.totp_nonce,
                    key_version=user.key_version,
                ),
                associated_data=f"admin:{user.username}",
            )
        except (InvalidTag, ValueError, TypeError):
            return False
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
