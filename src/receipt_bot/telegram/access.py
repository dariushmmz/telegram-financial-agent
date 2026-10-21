from __future__ import annotations

import re
from dataclasses import dataclass

from receipt_bot.persistence.models import TelegramGroup

GROUP_AUTHORIZER_USERNAME_SETTING = "access_authorized_by_telegram_username"
GROUP_AUTHORIZER_USER_ID_SETTING = "access_authorized_by_telegram_user_id"
TELEGRAM_USERNAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass(frozen=True, slots=True)
class TelegramAccessPolicy:
    """Environment-backed bootstrap allowlist and approved-group policy."""

    allowed_usernames: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_usernames:
            raise ValueError("Telegram username allowlist cannot be empty")
        if any(
            item != normalize_telegram_username(item)
            or TELEGRAM_USERNAME_PATTERN.fullmatch(item) is None
            for item in self.allowed_usernames
        ):
            raise ValueError("Telegram username allowlist must contain normalized usernames")

    def allows_user(self, telegram_username: str | None) -> bool:
        return normalize_telegram_username(telegram_username) in self.allowed_usernames

    def authorizes_group(self, group: TelegramGroup | None) -> bool:
        if group is None or not group.active or not isinstance(group.settings, dict):
            return False
        authorizer = group.settings.get(GROUP_AUTHORIZER_USERNAME_SETTING)
        return isinstance(authorizer, str) and self.allows_user(authorizer)

    def authorized_settings(
        self,
        settings: dict | None,
        telegram_username: str,
        telegram_user_id: int,
    ) -> dict:
        normalized = normalize_telegram_username(telegram_username)
        if normalized not in self.allowed_usernames:
            raise PermissionError("Telegram user is not allowlisted")
        return {
            **(settings or {}),
            GROUP_AUTHORIZER_USERNAME_SETTING: normalized,
            GROUP_AUTHORIZER_USER_ID_SETTING: telegram_user_id,
        }


def normalize_telegram_username(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().removeprefix("@").casefold()
