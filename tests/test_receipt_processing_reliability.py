from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway
from test_telegram_handler import activate, group_message, user
from test_telegram_worker import enqueue_photo, image_bytes
from test_text_receipts import CARD_TEXT

from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.errors import ProviderRateLimitError, ProviderTimeoutError
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.schema import ProviderReceipt, ProviderResult
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.models import (
    ExtractionAttempt,
    Payment,
    ReceiptJob,
    TelegramGroup,
    TelegramMessage,
    TelegramUser,
)
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram.financial import FinancialWorkflowService, Period
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.worker import ReceiptJobWorker


class RecordingProvider:
    name = "test-provider"
    model = "test-model"

    def __init__(self, events=None) -> None:
        self.events = list(events or [])
        self.calls: list[str] = []

    def _result(self) -> ProviderResult:
        candidate = ProviderReceipt(amount_value="۱۰۰۰", amount_unit="toman")
        return ProviderResult(
            provider=self.name,
            model=self.model,
            candidate=candidate,
            raw_response=candidate.model_dump(mode="json"),
        )

    def _next(self, kind: str) -> ProviderResult:
        self.calls.append(kind)
        event = self.events.pop(0) if self.events else None
        if isinstance(event, Exception):
            raise event
        return self._result()

    def extract(self, image: PreparedImage) -> ProviderResult:
        return self._next("image")

    def extract_text(self, text: str) -> ProviderResult:
        return self._next("text")


def worker(database, gateway, tmp_path, provider, **overrides) -> ReceiptJobWorker:
    options = {
        "retry_base_seconds": 1,
        "retry_max_seconds": 60,
        "retry_jitter_ratio": 0,
        "provider_cooldown_seconds": 10,
        "delay_notification_seconds": 3600,
    }
    options.update(overrides)
    return ReceiptJobWorker(
        database,
        gateway,
        ManagedStorage(tmp_path / "receipts"),
        ExtractionService(provider),
        access_policy=TEST_ACCESS_POLICY,
        **options,
    )


def make_image_job(database, gateway, *, chat_id: int, handler, unique: str) -> None:
    gateway.downloads[f"file-{unique}"] = image_bytes()
    enqueue_photo(
        handler,
        update_id=int(unique) + 10,
        message_id=int(unique) + 10,
        chat_id=chat_id,
        actor_id=2,
        file_id=f"file-{unique}",
        unique_id=f"unique-{unique}",
    )


def test_rate_limit_is_bounded_and_shared_cooldown_blocks_extra_calls(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    activate(handler, gateway, chat_id=-7101, actor_id=1)
    make_image_job(migrated_database, gateway, chat_id=-7101, handler=handler, unique="1")
    provider = RecordingProvider(
        [
            ProviderRateLimitError("limited"),
            ProviderRateLimitError("limited"),
            ProviderRateLimitError("limited"),
        ]
    )
    clock = [100.0]
    receipt_worker = worker(
        migrated_database,
        gateway,
        tmp_path,
        provider,
        max_extraction_attempts=3,
        clock=lambda: clock[0],
    )

    assert asyncio.run(receipt_worker.run_once()) is True  # download
    assert asyncio.run(receipt_worker.run_once()) is True  # first 429
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        job.available_at = utc_now()
    assert asyncio.run(receipt_worker.run_once()) is False
    assert provider.calls == ["image"]

    for expected_calls in (2, 3):
        clock[0] += 120
        with migrated_database.session() as session:
            session.scalar(select(ReceiptJob)).available_at = utc_now()
        assert asyncio.run(receipt_worker.run_once()) is True
        assert len(provider.calls) == expected_calls

    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        assert job.status == "failed_final"
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 3
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_temporary_failure_recovers_once_without_duplicate_payment(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    activate(handler, gateway, chat_id=-7102, actor_id=1)
    make_image_job(migrated_database, gateway, chat_id=-7102, handler=handler, unique="2")
    provider = RecordingProvider([ProviderTimeoutError("timeout"), None])
    receipt_worker = worker(migrated_database, gateway, tmp_path, provider)

    asyncio.run(receipt_worker.run_once())
    asyncio.run(receipt_worker.run_once())
    with migrated_database.session() as session:
        session.scalar(select(ReceiptJob)).available_at = utc_now()
    asyncio.run(receipt_worker.run_once())

    with migrated_database.session() as session:
        assert session.scalar(select(ReceiptJob.status)) == "succeeded"
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 2
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
    assert provider.calls == ["image", "image"]


def test_fair_scheduler_attempts_text_before_queued_image_extraction(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    activate(handler, gateway, chat_id=-7103, actor_id=1)
    make_image_job(migrated_database, gateway, chat_id=-7103, handler=handler, unique="3")
    asyncio.run(
        handler.handle(
            group_message(50, 50, chat_id=-7103, actor=user(2), text=CARD_TEXT)
        )
    )
    provider = RecordingProvider()
    receipt_worker = worker(migrated_database, gateway, tmp_path, provider)

    asyncio.run(receipt_worker.run_once())  # image download; next preference is text
    asyncio.run(receipt_worker.run_once())
    asyncio.run(receipt_worker.run_once())
    assert provider.calls == ["text", "image"]


def test_existing_over_limit_job_becomes_terminal_without_provider_replay(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    activate(handler, gateway, chat_id=-7104, actor_id=1)
    make_image_job(migrated_database, gateway, chat_id=-7104, handler=handler, unique="4")
    first_provider = RecordingProvider([ProviderTimeoutError("timeout")])
    first_worker = worker(migrated_database, gateway, tmp_path, first_provider)
    asyncio.run(first_worker.run_once())
    asyncio.run(first_worker.run_once())

    replacement_provider = RecordingProvider()
    restarted_worker = worker(
        migrated_database,
        gateway,
        tmp_path,
        replacement_provider,
        max_extraction_attempts=1,
    )
    with migrated_database.session() as session:
        session.scalar(select(ReceiptJob)).available_at = utc_now()
    assert asyncio.run(restarted_worker.run_once()) is True
    assert replacement_provider.calls == []
    with migrated_database.session() as session:
        assert session.scalar(select(ReceiptJob.status)) == "failed_final"
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 1


def test_receipt_views_show_all_processing_states_and_report_incomplete_warning(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    chat_id = -7105
    activate(handler, gateway, chat_id=chat_id, actor_id=1)
    for unique in ("5", "6", "7", "8"):
        make_image_job(
            migrated_database, gateway, chat_id=chat_id, handler=handler, unique=unique
        )
    with migrated_database.session() as session:
        group = session.scalar(
            select(TelegramGroup).where(TelegramGroup.telegram_chat_id == chat_id)
        )
        actor = session.scalar(
            select(TelegramUser).where(TelegramUser.telegram_user_id == 1)
        )
        jobs = list(session.scalars(select(ReceiptJob).order_by(ReceiptJob.id)))
        for job in jobs:
            session.get(TelegramMessage, job.source_message_id).occurred_at = utc_now()
        jobs[0].status = "queued_download"
        jobs[1].status = "extracting"
        jobs[2].status = "extraction_failed_retryable"
        jobs[3].status = "failed_final"
        jobs[3].finished_at = utc_now()
        group_id, actor_id = group.id, actor.id

    activate(handler, gateway, chat_id=-7199, actor_id=7, update_id=100)
    make_image_job(
        migrated_database, gateway, chat_id=-7199, handler=handler, unique="11"
    )
    with migrated_database.session() as session:
        other_group = session.scalar(
            select(TelegramGroup).where(TelegramGroup.telegram_chat_id == -7199)
        )
        other_job = session.scalar(
            select(ReceiptJob).where(ReceiptJob.group_id == other_group.id)
        )
        other_job.status = "failed_final"
        other_job.finished_at = utc_now()
        session.get(TelegramMessage, other_job.source_message_id).occurred_at = utc_now()

    finance = FinancialWorkflowService(migrated_database)
    period = Period.today()
    receipts, payment_ids, _, _ = finance.receipts(group_id, period)
    assert payment_ids == []
    assert "در صف دریافت تصویر" in receipts
    assert "در حال استخراج اطلاعات" in receipts
    assert "تلاش مجدد پس از اختلال پردازش" in receipts
    assert "پردازش ناموفق نهایی" in receipts
    pending, _, _, _, _ = finance.pending(group_id)
    assert "ورودی تصویری" in pending
    assert "پردازش ناموفق نهایی" in pending

    summary = finance.summary(group_id, period)
    report = finance.report(group_id, actor_id, period)
    for output in (summary, report):
        assert "هشدار کامل‌بودن گزارش" in output
        assert "جمع‌ها ممکن است ناقص باشند" in output
        assert "در صف ۱" in output
        assert "در حال پردازش ۱" in output
        assert "تلاش مجدد ۱" in output
        assert "ناموفق ۱" in output


def test_delayed_and_failure_notifications_are_sent_only_once(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    activate(handler, gateway, chat_id=-7106, actor_id=1)
    make_image_job(migrated_database, gateway, chat_id=-7106, handler=handler, unique="9")
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        job.status = "failed_final"
        job.finished_at = utc_now()
        job.created_at = utc_now() - timedelta(hours=2)

    receipt_worker = worker(migrated_database, gateway, tmp_path, RecordingProvider())
    asyncio.run(receipt_worker.run_once())
    asyncio.run(receipt_worker.run_once())
    failure_messages = [
        item for item in gateway.sent if "پس از چند تلاش به نتیجه نرسید" in item["text"]
    ]
    assert len(failure_messages) == 1
    assert failure_messages[0]["chat_id"] == 1
    assert "provider" not in failure_messages[0]["text"].casefold()

    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        assert job.failure_notified_at is not None

    make_image_job(
        migrated_database, gateway, chat_id=-7106, handler=handler, unique="10"
    )
    with migrated_database.session() as session:
        delayed_job = session.scalar(
            select(ReceiptJob).where(ReceiptJob.telegram_file_unique_id == "unique-10")
        )
        delayed_job.created_at = utc_now() - timedelta(hours=2)
    delayed_worker = worker(
        migrated_database,
        gateway,
        tmp_path,
        RecordingProvider(),
        delay_notification_seconds=60,
    )
    asyncio.run(delayed_worker.run_once())
    asyncio.run(delayed_worker.run_once())
    delayed_messages = [
        item for item in gateway.sent if "بیش از زمان معمول طول کشیده" in item["text"]
    ]
    assert len(delayed_messages) == 1
