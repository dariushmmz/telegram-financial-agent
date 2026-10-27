from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    # Durable worker retries own production recovery. Provider adapters default
    # to one call so their retries cannot multiply worker attempts.
    max_attempts: int = 1
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay_seconds < 0:
            raise ValueError("retry policy values are invalid")

    def delay(self, attempt: int) -> float:
        return self.base_delay_seconds * (2 ** (attempt - 1))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max((target.astimezone(UTC) - current.astimezone(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
