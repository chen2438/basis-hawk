from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BASIS_HAWK_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./basis-hawk.db"
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
