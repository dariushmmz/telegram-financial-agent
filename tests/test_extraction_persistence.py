from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from receipt_bot.extraction.image import PreparedImage, prepare_image
from receipt_bot.extraction.schema import ProviderReceipt, ProviderResult
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.models import ReceiptImage
from receipt_bot.persistence.repositories import (
    ExtractionAttemptRepository,
    GroupRepository,
    MessageRepository,
)


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


class StaticProvider:
    name = "static"

    def extract(self, image: PreparedImage) -> ProviderResult:
        candidate = ProviderReceipt(amount_value="۱۰۰۰", amount_unit="toman")
        return ProviderResult(
            provider=self.name,
            model="static-v1",
            candidate=candidate,
            raw_response=candidate.model_dump(mode="json"),
        )


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (20, 100, 180))
    image.putpixel((0, 0), (255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def seed_receipt(session, chat_id: int, file_unique_id: str):
    group = GroupRepository(session).add(chat_id, f"group-{chat_id}")
    message, _ = MessageRepository(session).add_idempotent(
        group_id=group.id,
        telegram_message_id=1,
        telegram_update_id=abs(chat_id),
        occurred_at=NOW,
        message_type="photo",
    )
    prepared = prepare_image(_image_bytes())
    receipt = ReceiptImage(
        group_id=group.id,
        source_message_id=message.id,
        telegram_file_id=f"file-{file_unique_id}",
        telegram_file_unique_id=file_unique_id,
        mime_type="image/png",
        byte_size=len(prepared.data),
        sha256=prepared.original_sha256,
        storage_key=f"group_{group.id}/{file_unique_id}.png",
    )
    session.add(receipt)
    session.flush()
    return group, receipt


def test_success_and_failure_attempts_append_with_monotonic_numbers(migrated_database) -> None:
    outcome = ExtractionService(StaticProvider()).extract(
        _image_bytes(), declared_mime_type="image/png"
    )
    with migrated_database.session() as session:
        group, receipt = seed_receipt(session, -4101, "unique-a")
        repository = ExtractionAttemptRepository(session, group.id)
        success = repository.append_success(receipt_id=receipt.id, outcome=outcome)
        failure = repository.append_failure(
            receipt_id=receipt.id,
            provider="gemini",
            model="configured-model",
            error_code="provider_timeout",
            retryable=True,
        )
        assert success.attempt_number == 1
        assert success.status == "succeeded"
        assert success.raw_response["amount_value"] == "۱۰۰۰"
        assert success.normalized_candidate["amount_rial"] == 10_000
        assert failure.attempt_number == 2
        assert failure.status == "failed_retryable"
        assert failure.raw_response is None
        assert [item.id for item in repository.list_for_receipt(receipt.id)] == [success.id, failure.id]


def test_extraction_attempt_rows_are_database_immutable(migrated_database) -> None:
    outcome = ExtractionService(StaticProvider()).extract(_image_bytes())
    with migrated_database.session() as session:
        group, receipt = seed_receipt(session, -4201, "unique-b")
        attempt = ExtractionAttemptRepository(session, group.id).append_success(
            receipt_id=receipt.id,
            outcome=outcome,
        )
        attempt_id = attempt.id

    with pytest.raises(DatabaseError, match="append-only"):
        with migrated_database.session() as session:
            session.execute(
                text("UPDATE extraction_attempts SET model='tampered' WHERE id=:id"),
                {"id": attempt_id},
            )
    with pytest.raises(DatabaseError, match="append-only"):
        with migrated_database.session() as session:
            session.execute(
                text("DELETE FROM extraction_attempts WHERE id=:id"),
                {"id": attempt_id},
            )


def test_cross_group_extraction_attempt_is_rejected(migrated_database) -> None:
    outcome = ExtractionService(StaticProvider()).extract(_image_bytes())
    with migrated_database.session() as session:
        group_one, receipt = seed_receipt(session, -4301, "unique-c")
        group_two = GroupRepository(session).add(-4302, "other-group")
        receipt_id = receipt.id
        group_two_id = group_two.id

    with pytest.raises(IntegrityError):
        with migrated_database.session() as session:
            ExtractionAttemptRepository(session, group_two_id).append_success(
                receipt_id=receipt_id,
                outcome=outcome,
            )
