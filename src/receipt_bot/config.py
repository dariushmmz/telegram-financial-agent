from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class AppSettings(BaseSettings):
    """Validated process configuration. Secrets render as masked values."""

    model_config = SettingsConfigDict(
        env_prefix="RECEIPT_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str
    data_root: Path
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    retention_days: int = Field(default=183, ge=1, le=3650)
    timezone: Literal["Asia/Tehran"] = "Asia/Tehran"
    sensitive_debug: bool = False
    extraction_provider: Literal["gemini", "openrouter"] = "gemini"
    gemini_models: str = "gemini-3.8-flash"
    openrouter_model: str | None = None
    telegram_bot_username: str | None = None
    telegram_poll_timeout_seconds: int = Field(default=30, ge=1, le=50)
    interaction_ttl_minutes: int = Field(default=15, ge=1, le=120)
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=300)
    allowed_telegram_usernames: str

    telegram_bot_token: SecretStr | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")

    @field_validator("allowed_telegram_usernames", mode="before")
    @classmethod
    def validate_allowed_telegram_usernames(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Telegram username allowlist is required and cannot be empty")
        usernames = [item.strip().removeprefix("@").casefold() for item in value.split(",")]
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item) for item in usernames):
            raise ValueError("Telegram username allowlist must contain valid ASCII usernames")
        if len(set(usernames)) != len(usernames):
            raise ValueError("Telegram username allowlist contains duplicate usernames")
        return ",".join(usernames)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except Exception as exc:
            raise ValueError("database URL is invalid") from exc
        if url.get_backend_name() != "sqlite":
            raise ValueError("the MVP supports sqlite database URLs only")
        if not url.database:
            raise ValueError("sqlite database URL must name a database")
        return value

    @field_validator("data_root")
    @classmethod
    def validate_data_root(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve(strict=False)
        if resolved == Path(resolved.anchor):
            raise ValueError("data root cannot be a filesystem root")
        return resolved

    @property
    def receipt_storage_root(self) -> Path:
        return self.data_root / "receipts"

    @field_validator("telegram_bot_username")
    @classmethod
    def normalize_bot_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().removeprefix("@")
        if not normalized or not normalized.replace("_", "a").isalnum():
            raise ValueError("Telegram bot username is invalid")
        return normalized

    @property
    def configured_gemini_models(self) -> tuple[str, ...]:
        models = tuple(item.strip() for item in self.gemini_models.split(",") if item.strip())
        if not models:
            raise ValueError("at least one Gemini model must be configured")
        return models

    @property
    def telegram_username_allowlist(self) -> frozenset[str]:
        return frozenset(self.allowed_telegram_usernames.split(","))
