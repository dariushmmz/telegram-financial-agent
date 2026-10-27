from __future__ import annotations

import asyncio
import logging

from receipt_bot.config import AppSettings
from receipt_bot.extraction.errors import ExtractionConfigurationError
from receipt_bot.extraction.providers.factory import build_provider
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.logging import configure_logging
from receipt_bot.operations import prepare_runtime
from receipt_bot.persistence.database import Database
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram.access import TelegramAccessPolicy
from receipt_bot.telegram.api import TelegramAPIClient
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.polling import PollingService
from receipt_bot.telegram.worker import ReceiptJobWorker

LOGGER = logging.getLogger(__name__)


async def run_bot(settings: AppSettings, *, stop_event: asyncio.Event | None = None) -> None:
    if settings.telegram_bot_token is None:
        raise ExtractionConfigurationError("TELEGRAM_BOT_TOKEN is required")
    configure_logging(settings.log_level)
    health = prepare_runtime(settings)
    # Alembic's logging configuration is loaded while preparing the runtime.
    # Restore the application's sanitized JSON handler before polling and
    # worker tasks start so operational events remain visible in production.
    configure_logging(settings.log_level)
    LOGGER.info(
        "runtime ready",
        extra={
            "context": {
                "database_revision": health.revision,
                "integrity": health.integrity,
                "provider": settings.extraction_provider,
            }
        },
    )
    database = Database(settings.database_url)
    storage = ManagedStorage(settings.receipt_storage_root)
    storage.ensure_exists()
    gateway = TelegramAPIClient(settings.telegram_bot_token)
    try:
        access_policy = TelegramAccessPolicy(settings.telegram_username_allowlist)
        bot_username = settings.telegram_bot_username
        if not bot_username:
            identity = await gateway.get_me()
            bot_username = identity.get("username")
        extraction = ExtractionService(build_provider(settings))
        handler = TelegramUpdateHandler(
            database,
            gateway,
            access_policy=access_policy,
            bot_username=bot_username,
            interaction_ttl_minutes=settings.interaction_ttl_minutes,
            storage=storage,
        )
        worker = ReceiptJobWorker(
            database,
            gateway,
            storage,
            extraction,
            access_policy=access_policy,
            retry_base_seconds=settings.receipt_retry_base_seconds,
            retry_max_seconds=settings.receipt_retry_max_seconds,
            retry_jitter_ratio=settings.receipt_retry_jitter_ratio,
            max_download_attempts=settings.receipt_max_download_attempts,
            max_extraction_attempts=settings.receipt_max_extraction_attempts,
            provider_cooldown_seconds=settings.receipt_provider_cooldown_seconds,
            delay_notification_seconds=settings.receipt_delay_notification_seconds,
        )
        service = PollingService(
            database,
            gateway,
            handler,
            worker,
            poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
            shutdown_grace_seconds=settings.shutdown_grace_seconds,
        )
        await service.run(stop_event or asyncio.Event())
    finally:
        await gateway.close()
        database.dispose()
