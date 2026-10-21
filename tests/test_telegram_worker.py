from __future__ import annotations

import asyncio
import io
from datetime import timedelta

from PIL import Image
from sqlalchemy import func, select
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway
from test_telegram_handler import activate, group_message, user

from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.errors import ProviderTimeoutError
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.schema import ProviderReceipt, ProviderResult
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.models import (
    ExtractionAttempt,
    Payment,
    ReceiptImage,
    ReceiptJob,
    TelegramGroup,
)
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram.api import TelegramAPIError
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.worker import ReceiptJobWorker


class SequencedProvider:
    name = "static"
    model = "static-v1"

    def __init__(self, events=None):
        self.events = list(events or [])
        self.calls = 0

    def extract(self, image: PreparedImage) -> ProviderResult:
        self.calls += 1
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, Exception):
                raise event
        candidate = ProviderReceipt(amount_value="۱۰۰۰", amount_unit="toman")
        return ProviderResult(
            provider=self.name,
            model=self.model,
            candidate=candidate,
            raw_response=candidate.model_dump(mode="json"),
            request_id=f"static-{self.calls}",
        )


def image_bytes(*, compress_level: int = 6) -> bytes:
    image = Image.new("RGB", (32, 24))
    for x in range(32):
        for y in range(24):
            image.putpixel((x, y), ((x * 7) % 255, (y * 11) % 255, ((x + y) * 5) % 255))
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=compress_level)
    return output.getvalue()


def enqueue_photo(handler, *, update_id, message_id, chat_id, actor_id, file_id, unique_id):
    update = group_message(
        update_id,
        message_id,
        chat_id=chat_id,
        actor=user(actor_id),
        document={
            "file_id": file_id,
            "file_unique_id": unique_id,
            "file_size": 100,
            "mime_type": "image/png",
            "file_name": f"{unique_id}.png",
        },
    )
    asyncio.run(handler.handle(update))


def make_worker(database, gateway, tmp_path, provider, *, retry_delay_seconds=0):
    return ReceiptJobWorker(
        database,
        gateway,
        ManagedStorage(tmp_path / "receipts"),
        ExtractionService(provider),
        access_policy=TEST_ACCESS_POLICY,
        retry_delay_seconds=retry_delay_seconds,
    )


def test_download_and_extraction_are_separate_durable_stages(migrated_database, tmp_path) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2101, actor_id=1)
    gateway.downloads["file-a"] = image_bytes()
    enqueue_photo(
        handler,
        update_id=2,
        message_id=2,
        chat_id=-2101,
        actor_id=2,
        file_id="file-a",
        unique_id="unique-a",
    )
    provider = SequencedProvider()
    worker = make_worker(migrated_database, gateway, tmp_path, provider)

    assert asyncio.run(worker.run_once()) is True
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        receipt = session.scalar(select(ReceiptImage))
        assert job.status == "queued_extraction"
        assert receipt.storage_key
        assert ManagedStorage(tmp_path / "receipts").resolve_key(receipt.storage_key).is_file()
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 0

    assert asyncio.run(worker.run_once()) is True
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        attempt = session.scalar(select(ExtractionAttempt))
        assert job.status == "succeeded"
        assert attempt.status == "succeeded"
        assert attempt.normalized_candidate["amount_rial"] == 10_000
        payment = session.scalar(select(Payment))
        assert payment.evidence_id == attempt.evidence_id
        assert payment.amount_rial == 10_000
        assert payment.settlement_state == "not_checked"
    assert provider.calls == 1


def test_download_failure_retries_without_duplicate_records(migrated_database, tmp_path) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2201, actor_id=1)
    gateway.downloads["file-b"] = [
        TelegramAPIError("file_transport", retryable=True),
        image_bytes(),
    ]
    enqueue_photo(
        handler,
        update_id=2,
        message_id=2,
        chat_id=-2201,
        actor_id=2,
        file_id="file-b",
        unique_id="unique-b",
    )
    worker = make_worker(migrated_database, gateway, tmp_path, SequencedProvider())

    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        assert job.status == "download_failed_retryable"
        assert job.last_error_code == "file_transport"
        assert session.scalar(select(func.count()).select_from(ReceiptImage)) == 0
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ReceiptJob)) == 1
        assert session.scalar(select(func.count()).select_from(ReceiptImage)) == 1
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(ReceiptJob.status)) == "succeeded"


def test_stale_download_claim_is_requeued_after_restart(migrated_database, tmp_path) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2251, actor_id=1)
    enqueue_photo(
        handler,
        update_id=2,
        message_id=2,
        chat_id=-2251,
        actor_id=2,
        file_id="stale-file",
        unique_id="stale-unique",
    )
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        job.status = "downloading"
        job.processing_started_at = utc_now() - timedelta(minutes=30)
    worker = make_worker(migrated_database, gateway, tmp_path, SequencedProvider())
    assert worker.recover_stale(older_than_minutes=10) == 1
    with migrated_database.session() as session:
        job = session.scalar(select(ReceiptJob))
        assert job.status == "queued_download"
        assert job.processing_started_at is None


def test_extraction_failure_appends_attempt_then_resumes(migrated_database, tmp_path) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2301, actor_id=1)
    gateway.downloads["file-c"] = image_bytes()
    enqueue_photo(
        handler,
        update_id=2,
        message_id=2,
        chat_id=-2301,
        actor_id=2,
        file_id="file-c",
        unique_id="unique-c",
    )
    provider = SequencedProvider([ProviderTimeoutError("safe timeout"), None])
    worker = make_worker(migrated_database, gateway, tmp_path, provider)
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        assert session.scalar(select(ReceiptJob.status)) == "extraction_failed_retryable"
        first = session.scalar(select(ExtractionAttempt))
        assert first.status == "failed_retryable"
        assert first.error_class == "provider_timeout"
    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        attempts = list(session.scalars(select(ExtractionAttempt).order_by(ExtractionAttempt.id)))
        assert [item.status for item in attempts] == ["failed_retryable", "succeeded"]
        assert session.scalar(select(ReceiptJob.status)) == "succeeded"
        assert session.scalar(select(func.count()).select_from(ReceiptImage)) == 1


def test_exact_and_file_identity_duplicates_do_not_reextract(migrated_database, tmp_path) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2401, actor_id=1)
    content = image_bytes()
    gateway.downloads.update({"file-1": content, "file-2": content, "file-3": content})
    provider = SequencedProvider()
    worker = make_worker(migrated_database, gateway, tmp_path, provider)

    enqueue_photo(handler, update_id=2, message_id=2, chat_id=-2401, actor_id=2, file_id="file-1", unique_id="u-1")
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    enqueue_photo(handler, update_id=3, message_id=3, chat_id=-2401, actor_id=2, file_id="file-2", unique_id="u-2")
    asyncio.run(worker.run_once())
    enqueue_photo(handler, update_id=4, message_id=4, chat_id=-2401, actor_id=2, file_id="file-3", unique_id="u-1")
    asyncio.run(worker.run_once())

    with migrated_database.session() as session:
        receipts = list(session.scalars(select(ReceiptImage).order_by(ReceiptImage.id)))
        jobs = list(session.scalars(select(ReceiptJob).order_by(ReceiptJob.id)))
        assert len(receipts) == 2
        assert receipts[1].duplicate_of_receipt_id == receipts[0].id
        assert receipts[1].duplicate_disposition == "exact"
        assert receipts[1].storage_state == "deduplicated"
        assert receipts[1].storage_key is None
        assert [job.status for job in jobs] == ["succeeded", "duplicate_exact", "duplicate_exact"]
        assert jobs[2].receipt_id == receipts[0].id
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
    assert provider.calls == 1


def test_visual_candidate_is_flagged_but_extracted_and_never_crosses_groups(
    migrated_database, tmp_path
) -> None:
    first_bytes = image_bytes(compress_level=0)
    second_bytes = image_bytes(compress_level=9)
    assert first_bytes != second_bytes
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-2501, actor_id=1, update_id=1)
    activate(handler, gateway, chat_id=-2502, actor_id=2, update_id=2)
    gateway.downloads.update({"a": first_bytes, "b": second_bytes, "c": second_bytes})
    provider = SequencedProvider()
    worker = make_worker(migrated_database, gateway, tmp_path, provider)

    enqueue_photo(handler, update_id=3, message_id=3, chat_id=-2501, actor_id=3, file_id="a", unique_id="a-u")
    asyncio.run(worker.run_once()); asyncio.run(worker.run_once())
    enqueue_photo(handler, update_id=4, message_id=4, chat_id=-2501, actor_id=3, file_id="b", unique_id="b-u")
    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        group_one_id = session.scalar(
            select(TelegramGroup.id).where(TelegramGroup.telegram_chat_id == -2501)
        )
        group_one_receipts = list(
            session.scalars(
                select(ReceiptImage)
                .where(ReceiptImage.group_id == group_one_id)
                .order_by(ReceiptImage.id)
            )
        )
        assert group_one_receipts[1].duplicate_disposition == "candidate"
        assert group_one_receipts[1].duplicate_of_receipt_id == group_one_receipts[0].id
        candidate_job = session.scalar(
            select(ReceiptJob).where(
                ReceiptJob.group_id == group_one_id,
                ReceiptJob.telegram_file_unique_id == "b-u",
            )
        )
        assert candidate_job.status == "queued_extraction"
    asyncio.run(worker.run_once())

    enqueue_photo(handler, update_id=5, message_id=5, chat_id=-2502, actor_id=4, file_id="c", unique_id="c-u")
    asyncio.run(worker.run_once())
    with migrated_database.session() as session:
        group_two_receipt = session.scalar(
            select(ReceiptImage).join(TelegramGroup, ReceiptImage.group_id == TelegramGroup.id).where(
                TelegramGroup.telegram_chat_id == -2502
            )
        )
        assert group_two_receipt.duplicate_disposition is None
        assert group_two_receipt.duplicate_of_receipt_id is None
