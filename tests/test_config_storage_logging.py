from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from receipt_bot.config import AppSettings
from receipt_bot.logging import JsonFormatter, configure_logging, sanitize_text
from receipt_bot.storage import ManagedStorage, UnsafeStoragePath


def test_configuration_requires_database_and_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEIPT_BOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("RECEIPT_BOT_DATA_ROOT", raising=False)
    with pytest.raises(ValidationError) as error:
        AppSettings(_env_file=None)
    message = str(error.value)
    assert "database_url" in message
    assert "data_root" in message


def test_configuration_validates_and_masks_secrets(tmp_path: Path) -> None:
    secret = "super-secret-telegram-token"
    settings = AppSettings(
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        data_root=tmp_path / "managed",
        telegram_bot_token=secret,
    )
    assert settings.retention_days == 183
    assert settings.receipt_storage_root == (tmp_path / "managed" / "receipts").resolve()
    assert secret not in repr(settings)
    assert "**********" in repr(settings)


def test_configuration_rejects_unsupported_database_without_secret_leak(tmp_path: Path) -> None:
    secret = "do-not-print-this-token"
    with pytest.raises(ValidationError) as error:
        AppSettings(
            database_url="postgresql://localhost/example",
            data_root=tmp_path / "managed",
            telegram_bot_token=secret,
        )
    assert "supports sqlite" in str(error.value)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "key",
    ["", "../outside.jpg", "a/../../outside.jpg", "/absolute.jpg", r"..\outside.jpg", r"C:\outside.jpg"],
)
def test_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    with pytest.raises(UnsafeStoragePath):
        storage.resolve_key(key)


def test_storage_resolves_safe_key_under_root(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    expected = (tmp_path / "receipts" / "group-1" / "abc.jpg").resolve()
    assert storage.resolve_key("group-1/abc.jpg") == expected


def test_storage_atomic_write_and_read_stay_inside_root(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    destination = storage.write_bytes_atomic("group-1/ab/receipt.png", b"safe-bytes")
    assert destination == storage.resolve_key("group-1/ab/receipt.png")
    assert storage.read_bytes("group-1/ab/receipt.png") == b"safe-bytes"
    assert list(destination.parent.glob(".receipt-*.tmp")) == []


def test_storage_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    storage.ensure_exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = storage.root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(UnsafeStoragePath):
        storage.resolve_key("link/file.jpg")


def test_json_logging_redacts_sensitive_context() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "safe message", (), None)
    record.context = {  # type: ignore[attr-defined]
        "telegram_token": "secret-token",
        "nested": {"api_key": "secret-key", "group_id": 12},
    }
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "safe message"
    assert payload["context"]["telegram_token"] == "[REDACTED]"
    assert payload["context"]["nested"]["api_key"] == "[REDACTED]"
    assert payload["context"]["nested"]["group_id"] == 12


def test_transport_request_logging_is_suppressed_to_protect_url_tokens() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_message_and_exception_text_redact_common_credentials() -> None:
    telegram = "https://api.telegram.org/bot123456:very-secret/getMe"
    message = "GEMINI_API_KEY=private-key Authorization: Bearer private.bearer"
    sanitized = sanitize_text(f"{telegram} {message}")
    assert "123456:very-secret" not in sanitized
    assert "private-key" not in sanitized
    assert "private.bearer" not in sanitized
    assert sanitized.count("[REDACTED]") == 3
