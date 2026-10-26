from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceTrace:
    message_id: int
    telegram_chat_id: int
    telegram_message_id: int
    sender_user_id: int | None = None
    sender_display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RequestReportLine:
    request_id: int
    counterparty_id: int
    counterparty_name: str
    request_state: str
    requested_rial: int | None
    submitted_rial: int
    evidence_confirmed_rial: int
    confirmed_rial: int
    unverified_rial: int
    documented_difference_rial: int | None
    confirmed_difference_rial: int | None
    status: str
    allocation_ids: tuple[int, ...]
    payment_ids: tuple[int, ...]
    revision_ids: tuple[int, ...]
    source: SourceTrace | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnallocatedPaymentLine:
    payment_id: int
    counterparty_id: int | None
    counterparty_name: str | None
    amount_rial: int
    unallocated_rial: int
    evidence_state: str
    revision_ids: tuple[int, ...]
    source: SourceTrace | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromiseReportLine:
    promise_id: int
    counterparty_id: int
    counterparty_name: str
    amount_rial: int | None
    state: str
    request_id: int | None
    revision_ids: tuple[int, ...]
    source: SourceTrace | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportExclusion:
    code: str
    target_type: str
    target_id: int
    amount_rial: int | None
    source: SourceTrace | None


@dataclass(frozen=True, slots=True)
class ReportTotals:
    requested_rial: int
    submitted_rial: int
    evidence_confirmed_rial: int
    confirmed_rial: int
    unverified_rial: int
    documented_difference_rial: int
    promised_rial: int
    excluded_known_rial: int
    comparable_request_count: int
    unresolved_count: int


@dataclass(frozen=True, slots=True)
class ReportProjection:
    group_id: int
    group_title: str
    period_start: datetime
    period_end: datetime
    as_of: datetime
    calculation_version: str
    request_lines: tuple[RequestReportLine, ...]
    unallocated_payments: tuple[UnallocatedPaymentLine, ...]
    promises: tuple[PromiseReportLine, ...]
    exclusions: tuple[ReportExclusion, ...]
    totals: ReportTotals
    manifest: dict[str, Any]
    manifest_hash: str
