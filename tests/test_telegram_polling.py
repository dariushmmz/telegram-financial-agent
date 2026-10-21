from __future__ import annotations

import asyncio

from sqlalchemy import select
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway
from test_telegram_handler import group_message, user

from receipt_bot.persistence.models import TelegramUpdate
from receipt_bot.telegram import texts
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.polling import PollingService


class IdleWorker:
    def __init__(self) -> None:
        self.recovered = False
        self.materialized = False
        self.runs = 0

    def recover_stale(self):
        self.recovered = True
        return 0

    def materialize_extracted_payments(self):
        self.materialized = True
        return 0

    async def run_once(self):
        self.runs += 1
        return False


def test_polling_advances_offset_and_persists_update_before_acknowledgement(
    migrated_database,
) -> None:
    stop = asyncio.Event()

    class ControlledGateway(FakeTelegramGateway):
        def __init__(self):
            super().__init__()
            self.polls = []

        async def get_updates(self, *, offset, timeout, allowed_updates):
            self.polls.append(
                {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates}
            )
            if len(self.polls) == 1:
                return [
                    group_message(
                        50,
                        50,
                        chat_id=-3101,
                        actor=user(7),
                        text="/start",
                    )
                ]
            stop.set()
            return []

    gateway = ControlledGateway()
    gateway.set_member(-3101, 7, "administrator")
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    worker = IdleWorker()
    service = PollingService(
        migrated_database,
        gateway,
        handler,
        worker,  # type: ignore[arg-type]
        poll_timeout_seconds=5,
    )
    asyncio.run(service.run(stop))

    assert worker.recovered is True
    assert worker.materialized is True
    assert gateway.polls[0] == {
        "offset": None,
        "timeout": 5,
        "allowed_updates": ["message", "edited_message", "callback_query"],
    }
    assert gateway.polls[1]["offset"] == 51
    assert gateway.sent[-1]["text"] == texts.START_ACTIVATED
    with migrated_database.session() as session:
        update = session.scalar(select(TelegramUpdate))
        assert update.update_id == 50
        assert update.status == "completed"
        assert update.payload == {}


def test_graceful_shutdown_allows_inflight_worker_to_finish(migrated_database) -> None:
    stop = asyncio.Event()
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()

    class WaitingGateway(FakeTelegramGateway):
        async def get_updates(self, *, offset, timeout, allowed_updates):
            await stop.wait()
            return []

    class InflightWorker(IdleWorker):
        def __init__(self) -> None:
            super().__init__()
            self.finished = False
            self.cancelled = False

        async def run_once(self):
            worker_started.set()
            try:
                await release_worker.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.finished = True
            return True

    async def scenario() -> InflightWorker:
        gateway = WaitingGateway()
        handler = TelegramUpdateHandler(
            migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
        )
        worker = InflightWorker()
        service = PollingService(
            migrated_database,
            gateway,
            handler,
            worker,  # type: ignore[arg-type]
            poll_timeout_seconds=5,
            shutdown_grace_seconds=2,
        )
        task = asyncio.create_task(service.run(stop))
        await worker_started.wait()
        stop.set()
        await asyncio.sleep(0)
        assert worker.cancelled is False
        release_worker.set()
        await task
        return worker

    worker = asyncio.run(scenario())
    assert worker.finished is True
    assert worker.cancelled is False
