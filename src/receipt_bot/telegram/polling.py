from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from receipt_bot.persistence.database import Database
from receipt_bot.persistence.repositories import TelegramUpdateRepository
from receipt_bot.telegram.api import TelegramAPIError
from receipt_bot.telegram.contracts import TelegramGateway
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.worker import ReceiptJobWorker

LOGGER = logging.getLogger(__name__)


class PollingService:
    ALLOWED_UPDATES: ClassVar[list[str]] = ["message", "edited_message", "callback_query"]

    def __init__(
        self,
        database: Database,
        gateway: TelegramGateway,
        handler: TelegramUpdateHandler,
        worker: ReceiptJobWorker,
        *,
        poll_timeout_seconds: int = 30,
        shutdown_grace_seconds: int = 30,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.handler = handler
        self.worker = worker
        self.poll_timeout_seconds = poll_timeout_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds

    async def run(self, stop_event: asyncio.Event) -> None:
        await self._recover_updates()
        self.worker.recover_stale()
        self.worker.materialize_extracted_payments()
        poll_task = asyncio.create_task(self._poll_loop(stop_event))
        worker_task = asyncio.create_task(self._worker_loop(stop_event))
        try:
            await stop_event.wait()
        finally:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
            try:
                await asyncio.wait_for(worker_task, timeout=self.shutdown_grace_seconds)
            except TimeoutError:
                LOGGER.warning("worker did not stop within the shutdown grace period")
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)

    async def _recover_updates(self) -> None:
        with self.database.session() as session:
            payloads = TelegramUpdateRepository(session).pending_payloads()
        for payload in payloads:
            try:
                await self.handler.handle(payload)
            except Exception:
                LOGGER.exception("recoverable Telegram update failed during startup")

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        with self.database.session() as session:
            offset = TelegramUpdateRepository(session).next_offset()
        while not stop_event.is_set():
            try:
                updates = await self.gateway.get_updates(
                    offset=offset,
                    timeout=self.poll_timeout_seconds,
                    allowed_updates=self.ALLOWED_UPDATES,
                )
            except TelegramAPIError as exc:
                LOGGER.warning(
                    "Telegram polling failed",
                    extra={"context": {"error_code": exc.code, "retryable": exc.retryable}},
                )
                await asyncio.sleep(2 if exc.retryable else 10)
                continue
            for update in updates:
                update_id = update.get("update_id")
                try:
                    await self.handler.handle(update)
                except Exception:
                    LOGGER.exception(
                        "Telegram update handler failed",
                        extra={"context": {"update_id": update_id}},
                    )
                if isinstance(update_id, int):
                    offset = update_id + 1 if offset is None else max(offset, update_id + 1)

    async def _worker_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                worked = await self.worker.run_once()
            except Exception:
                LOGGER.exception("receipt job worker failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except TimeoutError:
                    pass
