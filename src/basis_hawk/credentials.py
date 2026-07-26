from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from basis_hawk.crypto import EncryptedPayload, SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.storage import Database, ExchangeCredentialRow


class ExchangeEnvironment(StrEnum):
    SANDBOX = "sandbox"
    LIVE = "live"


@dataclass(frozen=True)
class ExchangeSecrets:
    api_key: str
    api_secret: str
    passphrase: str | None = None


@dataclass(frozen=True)
class CredentialSummary:
    exchange: Exchange
    environment: ExchangeEnvironment
    label: str
    masked_api_key: str
    updated_at: datetime


def _associated_data(exchange: Exchange, environment: ExchangeEnvironment) -> str:
    return f"exchange:{exchange.value}:environment:{environment.value}"


def _mask_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return f"{api_key[:2]}…{api_key[-2:]}"
    return f"{api_key[:4]}…{api_key[-4:]}"


class CredentialService:
    passphrase_exchanges = {Exchange.OKX, Exchange.BITGET}

    def __init__(self, database: Database, cipher: SecretCipher) -> None:
        self.database = database
        self.cipher = cipher

    async def save(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        label: str,
        secrets: ExchangeSecrets,
        actor: str,
    ) -> CredentialSummary:
        label = label.strip()
        api_key = secrets.api_key.strip()
        api_secret = secrets.api_secret.strip()
        passphrase = secrets.passphrase.strip() if secrets.passphrase else None
        if not label:
            raise ValueError("label is required")
        if len(api_key) < 8 or len(api_secret) < 8:
            raise ValueError("API key and secret must each contain at least 8 characters")
        if exchange in self.passphrase_exchanges and not passphrase:
            raise ValueError(f"{exchange.value} credentials require a passphrase")
        encrypted = self.cipher.encrypt_json(
            {
                "api_key": api_key,
                "api_secret": api_secret,
                "passphrase": passphrase,
            },
            associated_data=_associated_data(exchange, environment),
        )
        row = await self.database.save_exchange_credential(
            exchange=exchange.value,
            environment=environment.value,
            label=label,
            masked_api_key=_mask_api_key(api_key),
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_version=encrypted.key_version,
        )
        await self.database.append_audit(
            "credential.saved",
            actor=actor,
            details={
                "exchange": exchange.value,
                "environment": environment.value,
                "label": label,
            },
        )
        return self._summary(row)

    async def load(
        self,
        exchange: Exchange,
        environment: ExchangeEnvironment,
    ) -> ExchangeSecrets | None:
        row = await self.database.exchange_credential(exchange.value, environment.value)
        if row is None:
            return None
        value = self.cipher.decrypt_json(
            EncryptedPayload(
                ciphertext=row.ciphertext,
                nonce=row.nonce,
                key_version=row.key_version,
            ),
            associated_data=_associated_data(exchange, environment),
        )
        return ExchangeSecrets(
            api_key=str(value["api_key"]),
            api_secret=str(value["api_secret"]),
            passphrase=(
                str(value["passphrase"])
                if value.get("passphrase") not in (None, "")
                else None
            ),
        )

    async def list(self) -> list[CredentialSummary]:
        return [
            self._summary(row)
            for row in await self.database.list_exchange_credentials()
        ]

    async def delete(
        self,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        *,
        actor: str,
    ) -> bool:
        deleted = await self.database.delete_exchange_credential(
            exchange.value,
            environment.value,
        )
        if deleted:
            await self.database.append_audit(
                "credential.deleted",
                actor=actor,
                details={
                    "exchange": exchange.value,
                    "environment": environment.value,
                },
            )
        return deleted

    @staticmethod
    def _summary(row: ExchangeCredentialRow) -> CredentialSummary:
        return CredentialSummary(
            exchange=Exchange(row.exchange),
            environment=ExchangeEnvironment(row.environment),
            label=row.label,
            masked_api_key=row.masked_api_key,
            updated_at=row.updated_at,
        )
