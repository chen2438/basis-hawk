from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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


class AccountCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    spot: bool | None = None
    perpetual: bool | None = None
    maker: bool | None = None
    protected_ioc: bool | None = None
    market: bool | None = None
    isolated_margin: bool | None = None
    cross_margin: bool | None = None
    adl_rank: bool | None = None
    internal_transfer: bool | None = None
    detected_at: datetime | None = None


class AccountFeeSchedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    spot_maker: Decimal | None = Field(default=None, ge=0, le=Decimal("0.1"))
    spot_taker: Decimal | None = Field(default=None, ge=0, le=Decimal("0.1"))
    perpetual_maker: Decimal | None = Field(default=None, ge=0, le=Decimal("0.1"))
    perpetual_taker: Decimal | None = Field(default=None, ge=0, le=Decimal("0.1"))
    source: Literal["actual", "manual", "default"] = "default"
    checked_at: datetime | None = None

    @field_serializer(
        "spot_maker",
        "spot_taker",
        "perpetual_maker",
        "perpetual_taker",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


@dataclass(frozen=True)
class CredentialSummary:
    id: str
    exchange: Exchange
    environment: ExchangeEnvironment
    label: str
    masked_api_key: str
    position_mode: Literal["one_way", "hedge"] | None
    is_default: bool
    scanner_default: bool
    capabilities: AccountCapabilities
    fees: AccountFeeSchedule
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
        existing = await self.database.exchange_credential(
            exchange.value,
            environment.value,
        )
        return await self._save_account(
            account_id=existing.id if existing is not None else None,
            exchange=exchange,
            environment=environment,
            label=label,
            secrets=secrets,
            actor=actor,
            trading_default=True,
            scanner_default=True if existing is None else existing.scanner_default,
            fees=None,
            event_type="credential.saved",
        )

    async def create_account(
        self,
        *,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        label: str,
        secrets: ExchangeSecrets,
        actor: str,
        trading_default: bool = False,
        scanner_default: bool = False,
        fees: AccountFeeSchedule | None = None,
    ) -> CredentialSummary:
        return await self._save_account(
            account_id=str(uuid.uuid4()),
            exchange=exchange,
            environment=environment,
            label=label,
            secrets=secrets,
            actor=actor,
            trading_default=trading_default,
            scanner_default=scanner_default,
            fees=fees,
            event_type="credential.account_created",
        )

    async def replace_account(
        self,
        account_id: str,
        *,
        label: str,
        secrets: ExchangeSecrets,
        actor: str,
        fees: AccountFeeSchedule | None = None,
    ) -> CredentialSummary:
        existing = await self.database.exchange_credential_by_id(account_id)
        if existing is None:
            raise ValueError("account is not configured")
        return await self._save_account(
            account_id=account_id,
            exchange=Exchange(existing.exchange),
            environment=ExchangeEnvironment(existing.environment),
            label=label,
            secrets=secrets,
            actor=actor,
            trading_default=existing.is_default,
            scanner_default=existing.scanner_default,
            fees=fees or self._fees(existing),
            event_type="credential.account_replaced",
        )

    async def _save_account(
        self,
        *,
        account_id: str | None,
        exchange: Exchange,
        environment: ExchangeEnvironment,
        label: str,
        secrets: ExchangeSecrets,
        actor: str,
        trading_default: bool,
        scanner_default: bool,
        fees: AccountFeeSchedule | None,
        event_type: str,
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
            credential_id=account_id,
            is_default=trading_default,
            scanner_default=scanner_default,
            fee_payload=(fees or AccountFeeSchedule()).model_dump_json(),
            reconciliation_reason="exchange credential configuration changed",
        )
        await self.database.append_audit(
            event_type,
            actor=actor,
            details={
                "account_id": row.id,
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

    async def load_by_id(self, account_id: str) -> ExchangeSecrets | None:
        row = await self.database.exchange_credential_by_id(account_id)
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

    async def summary(self, account_id: str) -> CredentialSummary | None:
        row = await self.database.exchange_credential_by_id(account_id)
        if row is None:
            return None
        value = self._decrypt_row(row)
        return self._summary(
            row,
            position_mode=(
                cast(Literal["one_way", "hedge"], value["position_mode"])
                if value.get("position_mode") in {"one_way", "hedge"}
                else None
            ),
        )

    async def set_defaults(
        self,
        account_id: str,
        *,
        trading_default: bool,
        scanner_default: bool,
        actor: str,
    ) -> CredentialSummary:
        updated = await self.database.set_exchange_credential_defaults(
            account_id,
            trading_default=trading_default,
            scanner_default=scanner_default,
            reconciliation_reason="exchange account defaults changed",
        )
        if updated is None:
            raise ValueError("account is not configured")
        await self.database.append_audit(
            "credential.defaults_updated",
            actor=actor,
            details={
                "account_id": account_id,
                "exchange": updated.exchange,
                "environment": updated.environment,
                "trading_default": trading_default,
                "scanner_default": scanner_default,
            },
        )
        summary = await self.summary(account_id)
        if summary is None:
            raise ValueError("account is not configured")
        return summary

    async def update_account_metadata(
        self,
        account_id: str,
        *,
        capabilities: AccountCapabilities | None = None,
        fees: AccountFeeSchedule | None = None,
        actor: str = "worker",
    ) -> CredentialSummary:
        row = await self.database.exchange_credential_by_id(account_id)
        if row is None:
            raise ValueError("account is not configured")
        secrets = await self.load_by_id(account_id)
        if secrets is None:
            raise ValueError("account is not configured")
        value = self._decrypt_row(row)
        encrypted = self.cipher.encrypt_json(
            value,
            associated_data=_associated_data(
                Exchange(row.exchange),
                ExchangeEnvironment(row.environment),
            ),
        )
        updated = await self.database.save_exchange_credential(
            exchange=row.exchange,
            environment=row.environment,
            label=row.label,
            masked_api_key=row.masked_api_key,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_version=encrypted.key_version,
            credential_id=row.id,
            is_default=row.is_default,
            scanner_default=row.scanner_default,
            capabilities_payload=(
                capabilities.model_dump_json()
                if capabilities is not None
                else row.capabilities_payload
            ),
            fee_payload=(
                fees.model_dump_json() if fees is not None else row.fee_payload
            ),
        )
        await self.database.append_audit(
            "credential.metadata_updated",
            actor=actor,
            details={
                "account_id": account_id,
                "capabilities_updated": capabilities is not None,
                "fees_updated": fees is not None,
            },
        )
        return self._summary(
            updated,
            position_mode=secrets.position_mode,
        )

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

    async def delete_account(
        self,
        account_id: str,
        *,
        actor: str,
    ) -> bool:
        row = await self.database.exchange_credential_by_id(account_id)
        if row is None:
            return False
        deleted = await self.database.delete_exchange_credential_by_id(
            account_id,
            reconciliation_reason="exchange credential configuration changed",
        )
        if deleted:
            await self.database.append_audit(
                "credential.account_deleted",
                actor=actor,
                details={
                    "account_id": account_id,
                    "exchange": row.exchange,
                    "environment": row.environment,
                },
            )
        return deleted

    @staticmethod
    def _capabilities(row: ExchangeCredentialRow) -> AccountCapabilities:
        try:
            return AccountCapabilities.model_validate_json(
                row.capabilities_payload or "{}"
            )
        except ValueError:
            return AccountCapabilities()

    @staticmethod
    def _fees(row: ExchangeCredentialRow) -> AccountFeeSchedule:
        try:
            return AccountFeeSchedule.model_validate_json(row.fee_payload or "{}")
        except ValueError:
            return AccountFeeSchedule(
                source="default",
                checked_at=datetime.now(UTC),
            )

    @staticmethod
    def _summary(
        row: ExchangeCredentialRow,
        *,
        position_mode: Literal["one_way", "hedge"] | None,
    ) -> CredentialSummary:
        return CredentialSummary(
            id=row.id,
            exchange=Exchange(row.exchange),
            environment=ExchangeEnvironment(row.environment),
            label=row.label,
            masked_api_key=row.masked_api_key,
            position_mode=position_mode,
            is_default=row.is_default,
            scanner_default=row.scanner_default,
            capabilities=CredentialService._capabilities(row),
            fees=CredentialService._fees(row),
            updated_at=row.updated_at,
        )
