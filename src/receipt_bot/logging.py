from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_PARTS = ("api_key", "apikey", "token", "secret", "password", "authorization")
_REDACTED = "[REDACTED]"
_MESSAGE_SECRET_PATTERNS = (
    (
        re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+", re.IGNORECASE),
        rf"\1{_REDACTED}",
    ),
    (
        re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        rf"\1 {_REDACTED}",
    ),
    (
        re.compile(
            r"\b((?:telegram_bot_token|gemini_api_key|openrouter_api_key|api[_-]?key|password)"
            r"\s*[=:]\s*)[^\s,;]+",
            re.IGNORECASE,
        ),
        rf"\1{_REDACTED}",
    ),
)


def sanitize_text(value: str) -> str:
    sanitized = value
    for pattern, replacement in _MESSAGE_SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def redact(value: Any, key: str = "") -> Any:
    if any(part in key.casefold() for part in _SENSITIVE_PARTS):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_text(record.getMessage()),
        }
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload["context"] = redact(context)
        if record.exc_info:
            payload["exception"] = sanitize_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # HTTPX request logs include the full Telegram Bot API URL, whose path
    # contains the bot token. Transport details are mapped to safe app errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
