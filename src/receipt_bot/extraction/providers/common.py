from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay_seconds < 0:
            raise ValueError("retry policy values are invalid")

    def delay(self, attempt: int) -> float:
        return self.base_delay_seconds * (2 ** (attempt - 1))
