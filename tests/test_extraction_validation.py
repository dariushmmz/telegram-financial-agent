from __future__ import annotations

import json
from pathlib import Path

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.extraction.legacy import migrate_legacy_result
from receipt_bot.extraction.schema import (
    Confidence,
    DocumentKind,
    FieldEvidence,
    ProviderReceipt,
    ReceiptField,
)
from receipt_bot.extraction.validation import iban_is_valid, normalize_receipt

VALID_IBAN = "IR960120000000007769786056"


def issue_codes(result) -> set[str]:
    return {issue.code for issue in result.validation_issues}


def test_printed_persian_rial_values_normalize_deterministically() -> None:
    result = normalize_receipt(
        ProviderReceipt(
            document_kind=DocumentKind.BANK_SCREENSHOT,
            source_account="۶۰۳۷-****-****-۱۲۳۴",
            destination_iban="IR۹۶ ۰۱۲۰ ۰۰۰۰ ۰۰۰۰ ۷۷۶۹ ۷۸۶۰ ۵۶",
            amount_value="۹۰۰٬۰۰۰٬۰۰۰",
            amount_unit=CurrencyUnit.RIAL,
            transaction_datetime="۱۴۰۵/۰۷/۱۸ - ۱۲:۱۳:۱۹",
            tracking_code="۱۴۰۵۰۷۱۸۰۵۴۲۰۰۰۶۹۴۴۶",
            evidence=[
                FieldEvidence(field=ReceiptField.AMOUNT_VALUE, confidence=Confidence.CERTAIN),
                FieldEvidence(field=ReceiptField.AMOUNT_UNIT, confidence=Confidence.CERTAIN),
            ],
        )
    )
    assert result.source_account == "6037-****-****-1234"
    assert result.destination_iban == VALID_IBAN
    assert result.amount_value == 900_000_000
    assert result.amount_rial == 900_000_000
    assert result.transaction_datetime == "1405/07/18 - 12:13:19"
    assert result.tracking_code == "14050718054200069446"
    assert result.requires_review is False


def test_arabic_indic_toman_converts_exactly() -> None:
    result = normalize_receipt(
        ProviderReceipt(amount_value="١٬١٠٠٬٠٠٠", amount_unit=CurrencyUnit.TOMAN)
    )
    assert result.amount_value == 1_100_000
    assert result.amount_rial == 11_000_000


def test_unknown_unit_is_preserved_and_never_converted() -> None:
    result = normalize_receipt(
        ProviderReceipt(amount_value="۶۰۰۰۰۰", amount_unit=CurrencyUnit.UNKNOWN)
    )
    assert result.amount_value == 600_000
    assert result.amount_unit is CurrencyUnit.UNKNOWN
    assert result.amount_rial is None
    assert "currency_unit_unknown" in issue_codes(result)
    assert result.requires_review


def test_missing_and_malformed_amounts_require_review() -> None:
    missing = normalize_receipt(ProviderReceipt())
    malformed = normalize_receipt(
        ProviderReceipt(amount_value="۱۲٫۵ تومان", amount_unit=CurrencyUnit.TOMAN)
    )
    assert issue_codes(missing) == {"amount_missing"}
    assert "amount_invalid" in issue_codes(malformed)
    assert missing.amount_rial is None
    assert malformed.amount_rial is None


def test_invalid_identifiers_and_date_are_flagged_without_invention() -> None:
    result = normalize_receipt(
        ProviderReceipt(
            source_account="۱۲۳X",
            destination_iban="IR000000000000000000000000",
            amount_value="۱۰۰",
            amount_unit=CurrencyUnit.RIAL,
            transaction_datetime="۱۴۰۵/۱۳/۴۰ ۲۵:۷۰",
            tracking_code="۱۲A۳",
        )
    )
    assert {
        "source_account_invalid_characters",
        "iban_checksum_invalid",
        "transaction_datetime_invalid",
        "tracking_code_invalid",
    }.issubset(issue_codes(result))
    assert result.tracking_code == "12A3"


def test_handwritten_uncertainty_is_explicit() -> None:
    result = normalize_receipt(
        ProviderReceipt(
            document_kind=DocumentKind.HANDWRITTEN_FORM,
            is_handwritten=True,
            amount_value="۵۰۰۰۰۰",
            amount_unit=CurrencyUnit.TOMAN,
            evidence=[
                FieldEvidence(
                    field=ReceiptField.AMOUNT_VALUE,
                    confidence=Confidence.UNCERTAIN,
                    observed_text="۵۰۰۰۰۰؟",
                    note="دو رقم روی هم نوشته شده‌اند",
                )
            ],
        )
    )
    assert result.amount_rial == 5_000_000
    assert result.uncertain_fields == [ReceiptField.AMOUNT_VALUE]
    assert "field_uncertainty" in issue_codes(result)
    assert result.requires_review


def test_iban_checksum_accepts_normalized_persian_digits() -> None:
    assert iban_is_valid("IR۹۶۰۱۲۰۰۰۰۰۰۰۰۰۷۷۶۹۷۸۶۰۵۶")
    assert not iban_is_valid("IR960120000000007769786057")


def test_synthetic_legacy_json_migrates_without_ocr_accuracy_claim() -> None:
    fixture = Path(__file__).parent / "fixtures" / "legacy_receipt.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    migrated = migrate_legacy_result(payload)
    assert migrated.amount_unit is CurrencyUnit.RIAL
    assert migrated.amount_rial == payload["amount_rial"]
    assert migrated.destination_iban == VALID_IBAN
    assert migrated.document_kind is DocumentKind.BANK_SCREENSHOT
