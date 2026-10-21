from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from receipt_bot.domain.enums import CurrencyUnit


class DocumentKind(StrEnum):
    TELEGRAM_TEXT = "telegram_text"
    BANK_SCREENSHOT = "bank_screenshot"
    PRINTED_RECEIPT = "printed_receipt"
    PAPER_PHOTO = "paper_photo"
    HANDWRITTEN_FORM = "handwritten_form"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    CERTAIN = "certain"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"
    UNREADABLE = "unreadable"


class ReceiptField(StrEnum):
    SOURCE_ACCOUNT = "source_account"
    SOURCE_IBAN = "source_iban"
    SOURCE_CARD = "source_card"
    SOURCE_NAME = "source_name"
    DESTINATION_IBAN = "destination_iban"
    DESTINATION_CARD = "destination_card"
    DESTINATION_NAME = "destination_name"
    TRANSFER_METHOD = "transfer_method"
    SOURCE_BANK = "source_bank"
    PURPOSE = "purpose"
    AMOUNT_VALUE = "amount_value"
    AMOUNT_UNIT = "amount_unit"
    DESCRIPTION = "description"
    TRANSACTION_DATETIME = "transaction_datetime"
    TRACKING_CODE = "tracking_code"
    BANK_REFERENCE = "bank_reference"
    REFERENCE_NUMBER = "reference_number"
    DOCUMENT_NUMBER = "document_number"
    TRANSFER_STATUS = "transfer_status"
    SENDER_NOTE = "sender_note"
    PAYMENT_WARNING = "has_not_final_payment_warning"


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: ReceiptField
    confidence: Confidence
    observed_text: str | None = None
    note: str | None = None


class ProviderReceipt(BaseModel):
    """Provider-facing schema. Values remain close to the printed source."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_kind: DocumentKind = DocumentKind.UNKNOWN
    is_handwritten: bool | None = None
    source_account: str | None = None
    source_iban: str | None = None
    source_card: str | None = None
    source_name: str | None = None
    destination_iban: str | None = None
    destination_card: str | None = None
    destination_name: str | None = None
    transfer_method: str | None = None
    source_bank: str | None = None
    purpose: str | None = None
    amount_value: str | None = Field(
        default=None,
        description="Printed amount as text; retain separators and never infer a missing unit",
    )
    amount_unit: CurrencyUnit = CurrencyUnit.UNKNOWN
    description: str | None = None
    transaction_datetime: str | None = None
    tracking_code: str | None = None
    bank_reference: str | None = None
    reference_number: str | None = None
    document_number: str | None = None
    transfer_status: str | None = None
    sender_note: str | None = None
    has_not_final_payment_warning: bool | None = None
    evidence: list[FieldEvidence] = Field(default_factory=list)


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: IssueSeverity
    field: ReceiptField | None = None
    message: str


class NormalizedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_kind: DocumentKind
    is_handwritten: bool | None
    source_account: str | None
    source_iban: str | None
    source_card: str | None
    source_name: str | None
    destination_iban: str | None
    destination_card: str | None
    destination_name: str | None
    transfer_method: str | None
    source_bank: str | None
    purpose: str | None
    amount_source_text: str | None
    amount_value: int | None
    amount_unit: CurrencyUnit
    amount_rial: int | None
    description: str | None
    transaction_datetime: str | None
    tracking_code: str | None
    bank_reference: str | None
    reference_number: str | None
    document_number: str | None
    transfer_status: str | None
    sender_note: str | None
    has_not_final_payment_warning: bool | None
    evidence: list[FieldEvidence]
    validation_issues: list[ValidationIssue]
    uncertain_fields: list[ReceiptField]
    requires_review: bool


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    candidate: ProviderReceipt
    raw_response: dict[str, Any]
    usage: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ExtractionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_result: ProviderResult
    normalized: NormalizedReceipt
    source_kind: str = "image"
    content_sha256: str
    original_sha256: str | None = None
    sent_sha256: str | None = None
    sent_mime_type: str | None = None
    preprocessing: dict[str, Any]
