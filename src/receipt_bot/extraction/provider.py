from __future__ import annotations

from typing import Protocol

from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.schema import ProviderResult


class ReceiptExtractionProvider(Protocol):
    name: str

    def extract(self, image: PreparedImage) -> ProviderResult: ...

    def extract_text(self, text: str) -> ProviderResult: ...
