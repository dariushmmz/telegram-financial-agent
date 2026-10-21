import pytest

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import Money, format_rial_as_toman, normalize_digits, parse_integer


def test_normalize_both_persian_digit_sets() -> None:
    assert normalize_digits("شماره ۱۲۳ و ٤٥٦") == "شماره 123 و 456"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("۱٬۲۳۴٬۵۶۷", 1_234_567),
        ("١,٢٣٤,٥٦٧", 1_234_567),
        (" 1 234 567 ", 1_234_567),
        ("۰", 0),
    ],
)
def test_parse_integer_exactly(text: str, expected: int) -> None:
    assert parse_integer(text) == expected


@pytest.mark.parametrize("text", ["", "-1", "۱۲٫۵", "12.5", "۱۲ریال"])
def test_parse_integer_rejects_non_integer_values(text: str) -> None:
    with pytest.raises(ValueError):
        parse_integer(text)


def test_money_converts_known_units_and_preserves_unknown() -> None:
    assert Money(900, CurrencyUnit.RIAL).canonical_rial == 900
    assert Money(900, CurrencyUnit.TOMAN).canonical_rial == 9_000
    unknown = Money(900, CurrencyUnit.UNKNOWN)
    assert unknown.value == 900
    assert unknown.unit is CurrencyUnit.UNKNOWN
    assert unknown.canonical_rial is None


def test_money_rejects_floats_booleans_and_negative_values() -> None:
    with pytest.raises(TypeError):
        Money(1.5, CurrencyUnit.RIAL)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, CurrencyUnit.RIAL)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money(-1, CurrencyUnit.RIAL)


@pytest.mark.parametrize(
    ("rial", "persian", "ascii_value"),
    [
        (6_000_000_000, "۶۰۰٬۰۰۰٬۰۰۰", "600٬000٬000"),
        (1_234, "۱۲۳٫۴", "123٫4"),
        (-61_000_000, "-۶٬۱۰۰٬۰۰۰", "-6٬100٬000"),
    ],
)
def test_format_rial_as_exact_toman(rial: int, persian: str, ascii_value: str) -> None:
    assert format_rial_as_toman(rial) == persian
    assert format_rial_as_toman(rial, persian_digits=False) == ascii_value
