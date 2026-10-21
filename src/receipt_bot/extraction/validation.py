from __future__ import annotations

import re

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import Money, normalize_digits, parse_integer
from receipt_bot.extraction.schema import (
    Confidence,
    IssueSeverity,
    NormalizedReceipt,
    ProviderReceipt,
    ReceiptField,
    ValidationIssue,
)


MAX_DATABASE_INTEGER = 9_223_372_036_854_775_807
_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>\d{2,4})[/.\-](?P<month>\d{1,2})[/.\-](?P<day>\d{1,2})"
    r"(?:\s*[-–،,]?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
_PERSIAN_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+"
    r"(?P<month>فروردین|اردیبهشت|خرداد|تیر|مرداد|شهریور|مهر|آبان|آذر|دی|بهمن|اسفند)\s+"
    r"(?P<year>\d{4})(?!\d)"
)
_TIME_PATTERN = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?!\d)")
_ACCOUNT_PATTERN = re.compile(r"^[0-9*\-/. ]+$")


def iban_is_valid(iban: str) -> bool:
    normalized = normalize_digits(iban).replace(" ", "").replace("-", "").upper()
    if len(normalized) != 26 or not normalized.startswith("IR") or not normalized[2:].isdigit():
        return False
    rearranged = normalized[4:] + normalized[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _normalize_account(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    return normalize_digits(cleaned) if cleaned else None


def _normalize_iban(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_digits(value).replace(" ", "").replace("-", "").upper() or None


def _normalize_tracking(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_digits(value).replace(" ", "").replace("-", "") or None


def _normalize_card(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    return normalize_digits(cleaned) if cleaned else None


def _card_shape_is_valid(value: str) -> bool:
    compact = value.replace(" ", "").replace("-", "")
    if "*" in compact:
        return len(compact) <= 16 and compact.replace("*", "").isdigit()
    return len(compact) == 16 and compact.isdigit()


def _valid_printed_datetime(value: str) -> bool:
    match = _DATE_PATTERN.search(value)
    textual_match = _PERSIAN_DATE_PATTERN.search(value)
    if match is None and textual_match is None:
        return False
    if match is not None:
        month = int(match.group("month"))
        day = int(match.group("day"))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return False
        embedded_time = match if match.group("hour") is not None else None
    else:
        if not 1 <= int(textual_match.group("day")) <= 31:
            return False
        embedded_time = _TIME_PATTERN.search(value)
    if embedded_time is not None:
        hour = int(embedded_time.group("hour"))
        minute = int(embedded_time.group("minute"))
        second = int(embedded_time.group("second") or 0)
        if hour > 23 or minute > 59 or second > 59:
            return False
    return True


def normalize_receipt(candidate: ProviderReceipt) -> NormalizedReceipt:
    issues: list[ValidationIssue] = []
    amount_value: int | None = None
    amount_rial: int | None = None

    if candidate.amount_value is None:
        issues.append(
            ValidationIssue(
                code="amount_missing",
                severity=IssueSeverity.ERROR,
                field=ReceiptField.AMOUNT_VALUE,
                message="Receipt amount is missing or unreadable.",
            )
        )
    else:
        try:
            amount_value = parse_integer(candidate.amount_value)
            if amount_value == 0:
                raise ValueError("zero amount")
            if amount_value > MAX_DATABASE_INTEGER:
                raise ValueError("amount exceeds database range")
        except ValueError:
            issues.append(
                ValidationIssue(
                    code="amount_invalid",
                    severity=IssueSeverity.ERROR,
                    field=ReceiptField.AMOUNT_VALUE,
                    message="Receipt amount is not a valid positive integer.",
                )
            )
            amount_value = None

    if amount_value is not None:
        money = Money(amount_value, candidate.amount_unit)
        amount_rial = money.canonical_rial
        if amount_rial is not None and amount_rial > MAX_DATABASE_INTEGER:
            amount_rial = None
            issues.append(
                ValidationIssue(
                    code="amount_overflow",
                    severity=IssueSeverity.ERROR,
                    field=ReceiptField.AMOUNT_VALUE,
                    message="Canonical rial amount exceeds the supported range.",
                )
            )
        if candidate.amount_unit is CurrencyUnit.UNKNOWN:
            issues.append(
                ValidationIssue(
                    code="currency_unit_unknown",
                    severity=IssueSeverity.ERROR,
                    field=ReceiptField.AMOUNT_UNIT,
                    message="Rial versus toman is ambiguous; no conversion was performed.",
                )
            )

    source_account = _normalize_account(candidate.source_account)
    if source_account and not _ACCOUNT_PATTERN.fullmatch(source_account):
        issues.append(
            ValidationIssue(
                code="source_account_invalid_characters",
                severity=IssueSeverity.WARNING,
                field=ReceiptField.SOURCE_ACCOUNT,
                message="Source account contains unexpected characters.",
            )
        )

    source_iban = _normalize_iban(candidate.source_iban)
    if source_iban and not iban_is_valid(source_iban):
        issues.append(
            ValidationIssue(
                code="source_iban_checksum_invalid",
                severity=IssueSeverity.ERROR,
                field=ReceiptField.SOURCE_IBAN,
                message="Source IBAN failed deterministic checksum validation.",
            )
        )

    source_card = _normalize_card(candidate.source_card)
    if source_card and not _card_shape_is_valid(source_card):
        issues.append(
            ValidationIssue(
                code="source_card_invalid",
                severity=IssueSeverity.WARNING,
                field=ReceiptField.SOURCE_CARD,
                message="Source card has an invalid shape.",
            )
        )

    destination_iban = _normalize_iban(candidate.destination_iban)
    if destination_iban and not iban_is_valid(destination_iban):
        issues.append(
            ValidationIssue(
                code="iban_checksum_invalid",
                severity=IssueSeverity.ERROR,
                field=ReceiptField.DESTINATION_IBAN,
                message="Destination IBAN failed deterministic checksum validation.",
            )
        )

    destination_card = _normalize_card(candidate.destination_card)
    if destination_card and not _card_shape_is_valid(destination_card):
        issues.append(
            ValidationIssue(
                code="destination_card_invalid",
                severity=IssueSeverity.WARNING,
                field=ReceiptField.DESTINATION_CARD,
                message="Destination card has an invalid shape.",
            )
        )

    transaction_datetime = normalize_digits(_clean_text(candidate.transaction_datetime) or "") or None
    if transaction_datetime and not _valid_printed_datetime(transaction_datetime):
        issues.append(
            ValidationIssue(
                code="transaction_datetime_invalid",
                severity=IssueSeverity.WARNING,
                field=ReceiptField.TRANSACTION_DATETIME,
                message="Printed transaction date/time has an invalid or unsupported shape.",
            )
        )

    tracking_code = _normalize_tracking(candidate.tracking_code)
    if tracking_code and (not tracking_code.isascii() or not tracking_code.isdigit()):
        issues.append(
            ValidationIssue(
                code="tracking_code_invalid",
                severity=IssueSeverity.WARNING,
                field=ReceiptField.TRACKING_CODE,
                message="Tracking code is not digits-only after normalization.",
            )
        )

    reference_values = {
        ReceiptField.BANK_REFERENCE: _normalize_tracking(candidate.bank_reference),
        ReceiptField.REFERENCE_NUMBER: _normalize_tracking(candidate.reference_number),
        ReceiptField.DOCUMENT_NUMBER: _normalize_tracking(candidate.document_number),
    }
    for field, value in reference_values.items():
        if value and (not value.isascii() or not value.isdigit()):
            issues.append(
                ValidationIssue(
                    code=f"{field.value}_invalid",
                    severity=IssueSeverity.WARNING,
                    field=field,
                    message="Transaction reference is not digits-only after normalization.",
                )
            )

    uncertain_fields: list[ReceiptField] = []
    critical = {
        ReceiptField.AMOUNT_VALUE,
        ReceiptField.AMOUNT_UNIT,
        ReceiptField.DESTINATION_IBAN,
        ReceiptField.SOURCE_IBAN,
        ReceiptField.DESTINATION_CARD,
        ReceiptField.TRANSACTION_DATETIME,
        ReceiptField.TRACKING_CODE,
    }
    for evidence in candidate.evidence:
        if evidence.confidence in (Confidence.UNCERTAIN, Confidence.UNREADABLE) or (
            evidence.field in critical and evidence.confidence is Confidence.LIKELY
        ):
            if evidence.field not in uncertain_fields:
                uncertain_fields.append(evidence.field)

    if uncertain_fields:
        issues.append(
            ValidationIssue(
                code="field_uncertainty",
                severity=IssueSeverity.WARNING,
                message="One or more extracted fields require human review.",
            )
        )

    return NormalizedReceipt(
        document_kind=candidate.document_kind,
        is_handwritten=candidate.is_handwritten,
        source_account=source_account,
        source_iban=source_iban,
        source_card=source_card,
        source_name=_clean_text(candidate.source_name),
        destination_iban=destination_iban,
        destination_card=destination_card,
        destination_name=_clean_text(candidate.destination_name),
        transfer_method=_clean_text(candidate.transfer_method),
        source_bank=_clean_text(candidate.source_bank),
        purpose=_clean_text(candidate.purpose),
        amount_source_text=candidate.amount_value,
        amount_value=amount_value,
        amount_unit=candidate.amount_unit,
        amount_rial=amount_rial,
        description=_clean_text(candidate.description),
        transaction_datetime=transaction_datetime,
        tracking_code=tracking_code,
        bank_reference=reference_values[ReceiptField.BANK_REFERENCE],
        reference_number=reference_values[ReceiptField.REFERENCE_NUMBER],
        document_number=reference_values[ReceiptField.DOCUMENT_NUMBER],
        transfer_status=_clean_text(candidate.transfer_status),
        sender_note=_clean_text(candidate.sender_note),
        has_not_final_payment_warning=candidate.has_not_final_payment_warning,
        evidence=candidate.evidence,
        validation_issues=issues,
        uncertain_fields=uncertain_fields,
        requires_review=bool(issues),
    )
