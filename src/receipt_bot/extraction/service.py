from __future__ import annotations

import hashlib

from receipt_bot.extraction.image import ImageLimits, PreprocessOptions, prepare_image
from receipt_bot.extraction.provider import ReceiptExtractionProvider
from receipt_bot.extraction.schema import ExtractionOutcome
from receipt_bot.extraction.validation import normalize_receipt


class ExtractionService:
    def __init__(
        self,
        provider: ReceiptExtractionProvider,
        *,
        limits: ImageLimits | None = None,
    ) -> None:
        self.provider = provider
        self.limits = limits or ImageLimits()

    def extract(
        self,
        data: bytes,
        *,
        declared_mime_type: str | None = None,
        options: PreprocessOptions | None = None,
    ) -> ExtractionOutcome:
        prepared = prepare_image(
            data,
            declared_mime_type=declared_mime_type,
            limits=self.limits,
            options=options,
        )
        provider_result = self.provider.extract(prepared)
        normalized = normalize_receipt(provider_result.candidate)
        return ExtractionOutcome(
            provider_result=provider_result,
            normalized=normalized,
            source_kind="image",
            content_sha256=prepared.original_sha256,
            original_sha256=prepared.original_sha256,
            sent_sha256=prepared.sent_sha256,
            sent_mime_type=prepared.mime_type,
            preprocessing=prepared.record,
        )

    def extract_text(self, text: str) -> ExtractionOutcome:
        if not text or not text.strip():
            raise ValueError("receipt text must not be empty")
        provider_result = self.provider.extract_text(text)
        normalized = normalize_receipt(provider_result.candidate)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return ExtractionOutcome(
            provider_result=provider_result,
            normalized=normalized,
            source_kind="telegram_text",
            content_sha256=digest,
            preprocessing={"kind": "telegram_text", "character_count": len(text)},
        )
