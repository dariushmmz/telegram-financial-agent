from datetime import date, datetime

import pytest

from receipt_bot.domain.time import TEHRAN, UTC, require_aware, tehran_day_bounds, to_tehran


def test_tehran_day_bounds_are_utc_and_end_exclusive() -> None:
    start, end = tehran_day_bounds(date(2026, 10, 20))
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC
    assert start.isoformat() == "2026-10-19T20:30:00+00:00"
    assert end.isoformat() == "2026-10-20T20:30:00+00:00"


def test_timezone_conversion_requires_aware_datetime() -> None:
    with pytest.raises(ValueError):
        require_aware(datetime(2026, 10, 20))
    converted = to_tehran(datetime(2026, 10, 20, tzinfo=UTC))
    assert converted.tzinfo == TEHRAN
    assert converted.hour == 3
    assert converted.minute == 30
