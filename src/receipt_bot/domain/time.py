from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


TEHRAN = ZoneInfo("Asia/Tehran")
UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def to_tehran(value: datetime) -> datetime:
    return require_aware(value).astimezone(TEHRAN)


def tehran_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return the inclusive/exclusive UTC range for one Tehran calendar day."""
    start_local = datetime.combine(day, time.min, tzinfo=TEHRAN)
    end_local = datetime.combine(day.fromordinal(day.toordinal() + 1), time.min, tzinfo=TEHRAN)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
