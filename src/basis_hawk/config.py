from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BASIS_HAWK_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://basis_hawk:basis_hawk@127.0.0.1/basis_hawk"
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    auth_required: bool = True
    secure_cookies: bool = True
    session_hours: int = Field(default=12, ge=1, le=168)
    credential_master_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "smtps"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    notification_batch_size: int = Field(default=20, ge=1, le=100)
    backup_directory: Path = Path("/backups")
    transfer_per_request_limit_usdt: Decimal = Field(
        default=Decimal("0"),
        ge=0,
    )
    transfer_daily_limit_usdt: Decimal = Field(
        default=Decimal("0"),
        ge=0,
    )


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
