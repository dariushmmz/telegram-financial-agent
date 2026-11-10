from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

from receipt_bot.domain.enums import PaymentDateState
from receipt_bot.domain.money import normalize_digits
from receipt_bot.domain.time import TEHRAN, require_aware


_MONTHS = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "آذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}
_NUMERIC_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*[/.-]\s*(?P<month>\d{1,2})\s*[/.-]\s*(?P<day>\d{1,2})(?!\d)"
)
_TIME = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?!\d)")
_NAMED_DATE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+(?P<month>"
    + "|".join(_MONTHS)
    + r")\s+(?P<year>\d{4})(?!\d)"
)


@dataclass(frozen=True, slots=True)
class ParsedReceiptDate:
    value: datetime
    calendar: str


@dataclass(frozen=True, slots=True)
class ReceiptDateAssessment:
    parsed: datetime | None
    calendar: str | None
    state: PaymentDateState
    age_days: int | None
    max_age_days: int


def _jalali_to_gregorian_raw(year: int, month: int, day: int) -> date:
    jy = year + 1595
    days = (
        -355668
        + 365 * jy
        + (jy // 33) * 8
        + ((jy % 33 + 3) // 4)
        + day
        + ((month - 1) * 31 if month < 7 else (month - 7) * 30 + 186)
    )
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)
    month_days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 1
    for length in month_days:
        if gd <= length:
            break
        gd -= length
        gm += 1
    return date(gy, gm, gd)


def jalali_to_gregorian(year: int, month: int, day: int) -> date:
    if not 1 <= month <= 12:
        raise ValueError("invalid Jalali month")
    year_length = (
        _jalali_to_gregorian_raw(year + 1, 1, 1)
        - _jalali_to_gregorian_raw(year, 1, 1)
    ).days
    maximum = 31 if month <= 6 else 30 if month <= 11 else 30 if year_length == 366 else 29
    if not 1 <= day <= maximum:
        raise ValueError("invalid Jalali day")
    return _jalali_to_gregorian_raw(year, month, day)


def parse_receipt_datetime(value: str | None) -> ParsedReceiptDate | None:
    if not value or not value.strip():
        return None
    normalized = normalize_digits(value).replace("ي", "ی").replace("ك", "ک")
    numeric = _NUMERIC_DATE.search(normalized)
    named = _NAMED_DATE.search(normalized)
    if numeric:
        year = int(numeric.group("year"))
        month = int(numeric.group("month"))
        day = int(numeric.group("day"))
    elif named:
        year = int(named.group("year"))
        month = _MONTHS[named.group("month")]
        day = int(named.group("day"))
    else:
        return None
    clock = _TIME.search(normalized)
    hour = int(clock.group("hour")) if clock else 0
    minute = int(clock.group("minute")) if clock else 0
    second = int(clock.group("second") or 0) if clock else 0
    try:
        local_time = time(hour, minute, second)
        if 1200 <= year <= 1600:
            converted = jalali_to_gregorian(year, month, day)
            calendar = "jalali"
        elif 1900 <= year <= 2200:
            converted = date(year, month, day)
            calendar = "gregorian"
        else:
            return None
    except ValueError:
        return None
    return ParsedReceiptDate(
        value=datetime.combine(converted, local_time, tzinfo=TEHRAN),
        calendar=calendar,
    )


def assess_receipt_date(
    source: str | None,
    *,
    now: datetime,
    max_age_days: int,
) -> ReceiptDateAssessment:
    require_aware(now)
    if max_age_days < 0:
        raise ValueError("maximum receipt age cannot be negative")
    parsed = parse_receipt_datetime(source)
    if parsed is None:
        return ReceiptDateAssessment(
            parsed=None,
            calendar=None,
            state=PaymentDateState.NEEDS_REVIEW_UNKNOWN,
            age_days=None,
            max_age_days=max_age_days,
        )
    today = now.astimezone(TEHRAN).date()
    transaction_day = parsed.value.date()
    age_days = (today - transaction_day).days
    if age_days < 0:
        state = PaymentDateState.NEEDS_REVIEW_FUTURE
    elif age_days > max_age_days:
        state = PaymentDateState.NEEDS_REVIEW_OLD
    else:
        state = PaymentDateState.VALID
    return ReceiptDateAssessment(
        parsed=parsed.value,
        calendar=parsed.calendar,
        state=state,
        age_days=age_days,
        max_age_days=max_age_days,
    )
