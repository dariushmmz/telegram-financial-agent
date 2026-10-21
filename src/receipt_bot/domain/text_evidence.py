from __future__ import annotations

import hashlib
import re
import unicodedata

from receipt_bot.domain.money import normalize_digits


_BIDI_CONTROLS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))
_AMOUNT_WITH_UNIT = re.compile(
    r"مبلغ\s*[:：]?\s*[0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬،.\s]*\s*(?:ریال|تومان)"
)
_PAYMENT_ANCHORS = (
    "تراکنش موفق",
    "انتقال وجه",
    "کارت مقصد",
    "واریز به",
    "بلو به بلو",
    "شماره مرجع",
    "شماره ارجاع",
    "شماره سند",
    "شماره پیگیری",
)
_IR_IBAN = re.compile(r"(?:IR|آی\s*آر)\s*[-–—]?\s*[0-9۰-۹٠-٩][0-9۰-۹٠-٩\s-]{20,}", re.IGNORECASE)


def normalize_text_fingerprint(text: str) -> str:
    """Normalize harmless presentation differences without interpreting financial meaning."""
    value = unicodedata.normalize("NFKC", text).translate(_BIDI_CONTROLS)
    value = normalize_digits(value).replace("\u200c", " ").casefold()
    return " ".join(value.split())


def text_evidence_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text_fingerprint(text).encode("utf-8")).hexdigest()


def is_text_receipt_candidate(text: str | None) -> bool:
    """Conservatively recognize explicit payment evidence, not requests or promises."""
    if not text or text.startswith("/") or len(text) > 16_384:
        return False
    normalized = unicodedata.normalize("NFKC", text).translate(_BIDI_CONTROLS)
    if _AMOUNT_WITH_UNIT.search(normalized) is None:
        return False
    return any(anchor in normalized for anchor in _PAYMENT_ANCHORS) or _IR_IBAN.search(normalized) is not None
