from __future__ import annotations

import re
from dataclasses import dataclass, field

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import normalize_digits, parse_integer


_AMOUNT = re.compile(
    r"(?<!\d)([0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬_\s\u00a0\u202f]{0,30})\s*(ریال|تومان)(?!\w)",
    re.IGNORECASE,
)
_IBAN = re.compile(r"(?i)(?<![A-Z0-9])IR(?:[ \t-]*\d){24}(?!\d)")
_CARD_LABEL = re.compile(
    r"(?:شماره\s*)?(?:کارت|کارت\s*مقصد)[ \t]*[:：]?[ \t]*((?:\d[ \t-]*){16})(?!\d)",
    re.IGNORECASE,
)
_ACCOUNT_LABEL = re.compile(
    r"(?:شماره\s*)?(?:حساب|سپرده)[ \t]*[:：]?[ \t]*([0-9][0-9 .-]{4,30})(?!\d)",
    re.IGNORECASE,
)
_HOLDER = re.compile(
    r"(?:به\s*نام|بنام|نام\s*صاحب\s*(?:حساب|کارت))\s*[:：]?\s*([^\n\r،؛|]{2,100})",
    re.IGNORECASE,
)
_BANK = re.compile(r"بانک[ \t]+([آ-یA-Za-z][آ-یA-Za-z \t‌-]{1,40})")


@dataclass(frozen=True, slots=True)
class RequestAnnouncementCandidate:
    amount_value: int | None = None
    amount_unit: CurrencyUnit = CurrencyUnit.UNKNOWN
    account_holder: str | None = None
    destination_iban: str | None = None
    destination_card: str | None = None
    destination_account: str | None = None
    destination_bank: str | None = None
    ambiguous_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete_for_request(self) -> bool:
        return (
            self.amount_value is not None
            and self.amount_unit is not CurrencyUnit.UNKNOWN
            and self.account_holder is not None
            and bool(
                self.destination_iban
                or self.destination_card
                or self.destination_account
            )
            and not self.ambiguous_fields
        )

    def as_state(self) -> dict[str, object]:
        return {
            "amount_value": self.amount_value,
            "amount_unit": self.amount_unit.value,
            "account_holder": self.account_holder,
            "destination_iban": self.destination_iban,
            "destination_card": self.destination_card,
            "destination_account": self.destination_account,
            "destination_bank": self.destination_bank,
            "ambiguous_fields": list(self.ambiguous_fields),
        }


def _unique(values: list[str]) -> tuple[str | None, bool]:
    normalized = list(dict.fromkeys(item for item in values if item))
    return (normalized[0], False) if len(normalized) == 1 else (None, len(normalized) > 1)


def _valid_iranian_iban(value: str) -> bool:
    compact = re.sub(r"[\s-]", "", normalize_digits(value)).upper()
    if not re.fullmatch(r"IR\d{24}", compact):
        return False
    rearranged = compact[4:] + "1827" + compact[2:4]
    return int(rearranged) % 97 == 1


def _valid_card(value: str) -> bool:
    digits = re.sub(r"[\s-]", "", normalize_digits(value))
    if not re.fullmatch(r"\d{16}", digits) or len(set(digits)) == 1:
        return False
    total = 0
    for index, char in enumerate(digits):
        item = int(char) * (2 if index % 2 == 0 else 1)
        total += item - 9 if item > 9 else item
    return total % 10 == 0


def parse_request_announcement(text: str | None) -> RequestAnnouncementCandidate:
    source = normalize_digits(text or "").replace("ي", "ی").replace("ك", "ک")
    ambiguous: list[str] = []

    amounts: list[tuple[int, CurrencyUnit]] = []
    for match in _AMOUNT.finditer(source):
        try:
            value = parse_integer(match.group(1))
        except ValueError:
            continue
        unit = CurrencyUnit.RIAL if match.group(2).casefold() == "ریال" else CurrencyUnit.TOMAN
        amounts.append((value, unit))
    amounts = list(dict.fromkeys(amounts))
    if len(amounts) == 1:
        amount_value, amount_unit = amounts[0]
    else:
        amount_value, amount_unit = None, CurrencyUnit.UNKNOWN
        if len(amounts) > 1:
            ambiguous.append("amount")

    holders = [" ".join(match.group(1).strip(" .:-").split()) for match in _HOLDER.finditer(source)]
    account_holder, holder_ambiguous = _unique(holders)
    if holder_ambiguous:
        ambiguous.append("account_holder")

    ibans = []
    for match in _IBAN.finditer(source):
        compact = re.sub(r"[\s-]", "", match.group(0)).upper()
        if _valid_iranian_iban(compact):
            ibans.append(compact)
    destination_iban, iban_ambiguous = _unique(ibans)
    if iban_ambiguous:
        ambiguous.append("destination_iban")

    cards = []
    for match in _CARD_LABEL.finditer(source):
        compact = re.sub(r"[\s-]", "", match.group(1))
        if _valid_card(compact):
            cards.append(compact)
    destination_card, card_ambiguous = _unique(cards)
    if card_ambiguous:
        ambiguous.append("destination_card")

    accounts = []
    for match in _ACCOUNT_LABEL.finditer(source):
        raw = match.group(1).strip()
        if raw.upper().lstrip().startswith("IR"):
            continue
        compact = re.sub(r"[\s.-]", "", raw)
        if compact.isdigit() and 5 <= len(compact) <= 24:
            accounts.append(compact)
    destination_account, account_ambiguous = _unique(accounts)
    if account_ambiguous:
        ambiguous.append("destination_account")

    banks = [" ".join(match.group(1).strip().split()) for match in _BANK.finditer(source)]
    destination_bank, bank_ambiguous = _unique(banks)
    if bank_ambiguous:
        ambiguous.append("destination_bank")

    return RequestAnnouncementCandidate(
        amount_value=amount_value,
        amount_unit=amount_unit,
        account_holder=account_holder,
        destination_iban=destination_iban,
        destination_card=destination_card,
        destination_account=destination_account,
        destination_bank=destination_bank,
        ambiguous_fields=tuple(ambiguous),
    )


def normalize_request_identifier(kind: str, value: str) -> str:
    normalized_kind = kind.strip().casefold()
    normalized_value = normalize_digits(value).strip()
    if normalized_kind in {"شبا", "iban"}:
        compact = re.sub(r"[\s-]", "", normalized_value).upper()
        if not _valid_iranian_iban(compact):
            raise ValueError("invalid IBAN")
        return compact
    if normalized_kind in {"کارت", "card"}:
        compact = re.sub(r"[\s-]", "", normalized_value)
        if not _valid_card(compact):
            raise ValueError("invalid card")
        return compact
    if normalized_kind in {"حساب", "account"}:
        compact = re.sub(r"[\s.-]", "", normalized_value)
        if not compact.isdigit() or not 5 <= len(compact) <= 24:
            raise ValueError("invalid account")
        return compact
    if normalized_kind in {"بانک", "bank"}:
        compact = " ".join(normalized_value.split())
        if not 2 <= len(compact) <= 128:
            raise ValueError("invalid bank")
        return compact
    raise ValueError("unsupported account detail")
