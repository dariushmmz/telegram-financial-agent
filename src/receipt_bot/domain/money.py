from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from receipt_bot.domain.enums import CurrencyUnit


_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_GROUP_SEPARATORS = (",", "٬", "_", " ", "\u00a0", "\u202f")


def normalize_digits(value: str) -> str:
    """Normalize Persian and Arabic-Indic digits without changing other text."""
    return value.translate(_DIGIT_TRANSLATION)


def parse_integer(value: str) -> int:
    """Parse a non-negative, integer financial value with common group separators."""
    normalized = normalize_digits(value).strip()
    for separator in _GROUP_SEPARATORS:
        normalized = normalized.replace(separator, "")
    if not normalized or not normalized.isascii() or not normalized.isdecimal():
        raise ValueError("financial value must be a non-negative integer")
    return int(normalized)


@dataclass(frozen=True, slots=True)
class Money:
    """A source amount whose unit may intentionally remain unknown."""

    value: int
    unit: CurrencyUnit

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("money value must be an integer")
        if self.value < 0:
            raise ValueError("money value cannot be negative")

    @property
    def canonical_rial(self) -> int | None:
        if self.unit is CurrencyUnit.RIAL:
            return self.value
        if self.unit is CurrencyUnit.TOMAN:
            return self.value * 10
        return None

    @classmethod
    def from_text(cls, value: str, unit: CurrencyUnit) -> Money:
        return cls(parse_integer(value), unit)


def _group_ascii_integer(value: int) -> str:
    return f"{value:,}".replace(",", "٬")


def format_rial_as_toman(rial: int, *, persian_digits: bool = True) -> str:
    """Render exact rial as toman, retaining a single decimal when required."""
    if isinstance(rial, bool) or not isinstance(rial, int):
        raise TypeError("rial value must be an integer")
    toman = Decimal(rial) / Decimal(10)
    sign = "-" if toman < 0 else ""
    magnitude = abs(toman)
    whole = int(magnitude)
    fraction = int((magnitude - Decimal(whole)) * 10)
    rendered = f"{sign}{_group_ascii_integer(whole)}"
    if fraction:
        rendered += f"٫{fraction}"
    return rendered.translate(_PERSIAN_DIGITS) if persian_digits else rendered
