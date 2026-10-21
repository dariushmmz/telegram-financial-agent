from __future__ import annotations

from typing import Any

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.extraction.schema import DocumentKind, ProviderReceipt
from receipt_bot.extraction.validation import normalize_receipt


def migrate_legacy_result(payload: dict[str, Any]):
    """Convert the prototype JSON shape without asserting that its OCR was correct."""
    candidate = ProviderReceipt(
        document_kind=DocumentKind.BANK_SCREENSHOT,
        source_account=payload.get("source_account"),
        source_name=payload.get("source_name"),
        destination_iban=payload.get("destination_iban"),
        destination_name=payload.get("destination_name"),
        purpose=payload.get("purpose"),
        amount_value=str(payload["amount_rial"]) if payload.get("amount_rial") is not None else None,
        amount_unit=CurrencyUnit.RIAL if payload.get("amount_rial") is not None else CurrencyUnit.UNKNOWN,
        description=payload.get("description"),
        transaction_datetime=payload.get("transaction_datetime"),
        tracking_code=payload.get("tracking_code"),
        transfer_status=payload.get("transfer_status"),
        sender_note=payload.get("sender_note"),
        has_not_final_payment_warning=payload.get("has_not_final_payment_warning"),
        evidence=[],
    )
    return normalize_receipt(candidate)
