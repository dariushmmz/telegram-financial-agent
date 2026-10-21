from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.text_evidence import is_text_receipt_candidate, text_evidence_sha256
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.schema import DocumentKind, ProviderReceipt, ProviderResult
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.extraction.validation import normalize_receipt
from receipt_bot.persistence.models import (
    ExtractionAttempt,
    PaymentEvidence,
    Payment,
    ReceiptImage,
    TelegramGroup,
    TextReceiptJob,
)
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.worker import ReceiptJobWorker
from telegram_fakes import FakeTelegramGateway
from test_telegram_handler import activate, group_message, user


CARD_TEXT = """تراکنش موفق
انتقال وجه کارت‌به‌کارت
تاریخ 1405/07/28 13:31:33
کارت: **--**-9517
مبلغ : 3,446,000 ریال
کارت مقصد : 6104-3377-9501-2077
بنام : مهرناز ابراهیم زاده
شماره مرجع بانک: 366324228308
بانک صادر کننده مبدا: ملی
شماره امداد مشتریان: 021-83333
شماره ارجاع: 074260173337"""

IBAN_TEXT = """انتقال امیرمحمد محمدزاده قوژدی بلو به بلو
سپرده: IR - ۶۵ ۰۵۶۰ ۶۱۱۸ ۲۸۰۰ ۵۹۹۸ ۶۴۱۵ ۰۱
مبلغ: ۶۶۷٬۳۰۰ ریال
تاریخ: ۱۶:۱۷ سه‌شنبه، ۲۱ مهر ۱۴۰۵
واریز به: IR - ۳۸ ۰۵۶۰ ۶۱۱۸ ۲۸۰۰ ۷۴۶۷ ۸۵۸۵ ۰۱
به نام فاطمه محمدی
شماره سند: ۱۴۰۵۶۰۲۱۳۲۴۱۹۶"""


class TextProvider:
    name = "static"
    model = "static-text-v1"

    def __init__(self) -> None:
        self.text_calls: list[str] = []

    def extract(self, image: PreparedImage) -> ProviderResult:  # pragma: no cover - not used here
        raise AssertionError("image extraction was not expected")

    def extract_text(self, text: str) -> ProviderResult:
        self.text_calls.append(text)
        if "بلو به بلو" in text:
            candidate = ProviderReceipt(
                document_kind=DocumentKind.TELEGRAM_TEXT,
                is_handwritten=False,
                source_name="امیرمحمد محمدزاده قوژدی",
                source_iban="IR - ۶۵ ۰۵۶۰ ۶۱۱۸ ۲۸۰۰ ۵۹۹۸ ۶۴۱۵ ۰۱",
                destination_iban="IR - ۳۸ ۰۵۶۰ ۶۱۱۸ ۲۸۰۰ ۷۴۶۷ ۸۵۸۵ ۰۱",
                destination_name="فاطمه محمدی",
                transfer_method="بلو به بلو",
                amount_value="۶۶۷٬۳۰۰",
                amount_unit=CurrencyUnit.RIAL,
                transaction_datetime="۱۶:۱۷ سه‌شنبه، ۲۱ مهر ۱۴۰۵",
                document_number="۱۴۰۵۶۰۲۱۳۲۴۱۹۶",
            )
        else:
            candidate = ProviderReceipt(
                document_kind=DocumentKind.TELEGRAM_TEXT,
                is_handwritten=False,
                source_card="**--**-9517",
                destination_card="6104-3377-9501-2077",
                destination_name="مهرناز ابراهیم زاده",
                transfer_method="کارت‌به‌کارت",
                source_bank="ملی",
                amount_value="3,446,000",
                amount_unit=CurrencyUnit.RIAL,
                transaction_datetime="1405/07/28 13:31:33",
                bank_reference="366324228308",
                reference_number="074260173337",
                transfer_status="موفق",
            )
        return ProviderResult(
            provider=self.name,
            model=self.model,
            candidate=candidate,
            raw_response=candidate.model_dump(mode="json"),
        )


def test_candidate_filter_requires_explicit_amount_unit_and_payment_anchor() -> None:
    assert is_text_receipt_candidate(CARD_TEXT)
    assert is_text_receipt_candidate(IBAN_TEXT)
    assert not is_text_receipt_candidate("لطفاً مبلغ ۶۶۷٬۳۰۰ ریال را فردا پرداخت کنید")
    assert not is_text_receipt_candidate("انتقال وجه انجام شد ولی مبلغ مشخص نیست")
    assert text_evidence_sha256(CARD_TEXT) == text_evidence_sha256(CARD_TEXT.replace("3", "۳"))


def test_card_text_fields_normalize_without_treating_support_phone_as_reference() -> None:
    candidate = TextProvider().extract_text(CARD_TEXT).candidate
    result = normalize_receipt(candidate)
    assert result.document_kind is DocumentKind.TELEGRAM_TEXT
    assert result.amount_value == 3_446_000
    assert result.amount_rial == 3_446_000
    assert result.source_card == "**--**-9517"
    assert result.destination_card == "6104-3377-9501-2077"
    assert result.bank_reference == "366324228308"
    assert result.reference_number == "074260173337"
    assert result.tracking_code is None
    assert "83333" not in {result.bank_reference, result.reference_number}


def test_iban_text_fields_and_persian_named_date_normalize_deterministically() -> None:
    outcome = ExtractionService(TextProvider()).extract_text(IBAN_TEXT)
    result = outcome.normalized
    assert outcome.source_kind == "telegram_text"
    assert outcome.original_sha256 is None
    assert result.source_iban == "IR650560611828005998641501"
    assert result.destination_iban == "IR380560611828007467858501"
    assert result.amount_value == 667_300
    assert result.amount_rial == 667_300
    assert result.transaction_datetime == "16:17 سه‌شنبه، 21 مهر 1405"
    assert result.document_number == "14056021324196"
    assert not any(issue.code.endswith("iban_checksum_invalid") for issue in result.validation_issues)
    assert not any(issue.code == "transaction_datetime_invalid" for issue in result.validation_issues)


def test_text_receipt_pipeline_persists_evidence_extracts_and_isolates_duplicates(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway)
    activate(handler, gateway, chat_id=-3101, actor_id=1, update_id=1)
    activate(handler, gateway, chat_id=-3102, actor_id=2, update_id=2)

    asyncio.run(handler.handle(group_message(3, 30, chat_id=-3101, actor=user(3), text=CARD_TEXT)))
    asyncio.run(handler.handle(group_message(4, 31, chat_id=-3101, actor=user(3), text=CARD_TEXT)))
    asyncio.run(handler.handle(group_message(5, 50, chat_id=-3102, actor=user(4), text=CARD_TEXT)))

    with migrated_database.session() as session:
        jobs = list(session.scalars(select(TextReceiptJob).order_by(TextReceiptJob.id)))
        evidence = list(session.scalars(select(PaymentEvidence).order_by(PaymentEvidence.id)))
        assert [job.status for job in jobs] == ["queued_extraction", "duplicate_exact", "queued_extraction"]
        assert evidence[1].duplicate_of_evidence_id == evidence[0].id
        assert evidence[1].duplicate_disposition == "exact"
        assert evidence[2].duplicate_of_evidence_id is None
        assert evidence[0].group_id != evidence[2].group_id
        assert session.scalar(select(func.count()).select_from(ReceiptImage)) == 0

    provider = TextProvider()
    worker = ReceiptJobWorker(
        migrated_database,
        gateway,
        ManagedStorage(tmp_path / "receipts"),
        ExtractionService(provider),
        retry_delay_seconds=0,
    )
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False

    with migrated_database.session() as session:
        attempts = list(session.scalars(select(ExtractionAttempt).order_by(ExtractionAttempt.id)))
        groups = list(session.scalars(select(TelegramGroup).order_by(TelegramGroup.id)))
        assert len(attempts) == 2
        assert all(attempt.receipt_id is None for attempt in attempts)
        assert {attempt.group_id for attempt in attempts} == {group.id for group in groups}
        assert all(attempt.normalized_candidate["amount_rial"] == 3_446_000 for attempt in attempts)
        assert session.scalar(select(func.count()).select_from(PaymentEvidence)) == 3
        assert session.scalar(select(func.count()).select_from(Payment)) == 2
    assert provider.text_calls == [CARD_TEXT, CARD_TEXT]
