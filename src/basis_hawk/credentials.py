from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast

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
    position_mode: Literal["one_way", "hedge"] | None = None


@dataclass(frozen=True)
class CredentialSummary:
    exchange: Exchange
    environment: ExchangeEnvironment
    label: str
    masked_api_key: str
    position_mode: Literal["one_way", "hedge"] | None
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
        if exchange == Exchange.BYBIT and secrets.position_mode is None:
            raise ValueError("Bybit credentials require the configured position mode")
        if exchange != Exchange.BYBIT and secrets.position_mode is not None:
            raise ValueError("position mode is only configurable for Bybit")
        encrypted = self.cipher.encrypt_json(
            {
                "api_key": api_key,
                "api_secret": api_secret,
                "passphrase": passphrase,
                "position_mode": secrets.position_mode,
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
            reconciliation_reason="exchange credential configuration changed",
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
        return self._summary(row, position_mode=secrets.position_mode)

    async def load(
        self,
        exchange: Exchange,
        environment: ExchangeEnvironment,
    ) -> ExchangeSecrets | None:
        row = await self.database.exchange_credential(exchange.value, environment.value)
        if row is None:
            return None
        value = self._decrypt_row(row)
        return ExchangeSecrets(
            api_key=str(value["api_key"]),
            api_secret=str(value["api_secret"]),
            passphrase=(
                str(value["passphrase"])
                if value.get("passphrase") not in (None, "")
                else None
            ),
            position_mode=(
                cast(Literal["one_way", "hedge"], value["position_mode"])
                if value.get("position_mode") in {"one_way", "hedge"}
                else None
            ),
        )

    async def update_position_mode(
        self,
        environment: ExchangeEnvironment,
        *,
        position_mode: Literal["one_way", "hedge"],
        actor: str,
    ) -> CredentialSummary:
        row = await self.database.exchange_credential(
            Exchange.BYBIT.value,
            environment.value,
        )
        if row is None:
            raise ValueError("Bybit credentials are not configured")
        value = self._decrypt_row(row)
        value["position_mode"] = position_mode
        encrypted = self.cipher.encrypt_json(
            value,
            associated_data=_associated_data(Exchange.BYBIT, environment),
        )
        updated = await self.database.save_exchange_credential(
            exchange=Exchange.BYBIT.value,
            environment=environment.value,
            label=row.label,
            masked_api_key=row.masked_api_key,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_version=encrypted.key_version,
            reconciliation_reason="exchange credential configuration changed",
        )
        await self.database.append_audit(
            "credential.position_mode_updated",
            actor=actor,
            details={
                "exchange": Exchange.BYBIT.value,
                "environment": environment.value,
                "position_mode": position_mode,
            },
        )
        return self._summary(updated, position_mode=position_mode)

    def _decrypt_row(self, row: ExchangeCredentialRow) -> dict[str, object]:
        return self.cipher.decrypt_json(
            EncryptedPayload(
                ciphertext=row.ciphertext,
                nonce=row.nonce,
                key_version=row.key_version,
            ),
            associated_data=_associated_data(
                Exchange(row.exchange),
                ExchangeEnvironment(row.environment),
            ),
        )

    async def list(self) -> list[CredentialSummary]:
        summaries: list[CredentialSummary] = []
        for row in await self.database.list_exchange_credentials():
            value = self._decrypt_row(row)
            summaries.append(
                self._summary(
                    row,
                    position_mode=(
                        cast(
                            Literal["one_way", "hedge"],
                            value["position_mode"],
                        )
                        if value.get("position_mode") in {"one_way", "hedge"}
                        else None
                    ),
                )
            )
        return summaries

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
            reconciliation_reason="exchange credential configuration changed",
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
    def _summary(
        row: ExchangeCredentialRow,
        *,
        position_mode: Literal["one_way", "hedge"] | None,
    ) -> CredentialSummary:
        return CredentialSummary(
            exchange=Exchange(row.exchange),
            environment=ExchangeEnvironment(row.environment),
            label=row.label,
            masked_api_key=row.masked_api_key,
            position_mode=position_mode,
            updated_at=row.updated_at,
        )
