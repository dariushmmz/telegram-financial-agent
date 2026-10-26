from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from receipt_bot.domain.enums import (
    AllocationState,
    PaymentEvidenceState,
    PromiseState,
    RequestState,
)
from receipt_bot.domain.time import require_aware, utc_now
from receipt_bot.persistence.models import (
    Counterparty,
    Payment,
    PaymentPromise,
    PaymentRequest,
    RecordRevision,
    ReportRun,
    RequestPaymentAllocation,
    TelegramGroup,
    TelegramMessage,
    TelegramUser,
)
from receipt_bot.reconciliation.service import ACTIVE_ALLOCATION_STATES, LedgerNotFound
from receipt_bot.reporting.models import (
    PromiseReportLine,
    ReportExclusion,
    ReportProjection,
    ReportTotals,
    RequestReportLine,
    SourceTrace,
    UnallocatedPaymentLine,
)


CALCULATION_VERSION = "reconciliation-v2"
COUNTABLE_PAYMENT_STATES = {
    PaymentEvidenceState.SUBMITTED.value,
    PaymentEvidenceState.NEEDS_REVIEW.value,
    PaymentEvidenceState.CONFIRMED.value,
}


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ReportingEngine:
    """Builds deterministic projections from authoritative ledger rows; no AI is used."""

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id
        self.group = session.get(TelegramGroup, group_id)
        if self.group is None:
            raise LedgerNotFound("group does not exist")

    def _source(self, source_message_id: int | None) -> SourceTrace | None:
        if source_message_id is None:
            return None
        message = self.session.get(TelegramMessage, source_message_id)
        if message is None or message.group_id != self.group_id:
            return None
        sender = (
            self.session.get(TelegramUser, message.sender_user_id)
            if message.sender_user_id is not None
            else None
        )
        return SourceTrace(
            message_id=message.id,
            telegram_chat_id=self.group.telegram_chat_id,
            telegram_message_id=message.telegram_message_id,
            sender_user_id=sender.id if sender else None,
            sender_display_name=sender.display_name if sender else None,
        )

    def _revision_id(self, target_type: str, target_id: int, revision_number: int) -> int | None:
        return self.session.scalar(
            select(RecordRevision.id).where(
                RecordRevision.group_id == self.group_id,
                RecordRevision.target_type == target_type,
                RecordRevision.target_id == target_id,
                RecordRevision.revision_number == revision_number,
            )
        )

    def _active_allocations_for_request(self, request_id: int) -> list[RequestPaymentAllocation]:
        return list(
            self.session.scalars(
                select(RequestPaymentAllocation)
                .where(
                    RequestPaymentAllocation.group_id == self.group_id,
                    RequestPaymentAllocation.request_id == request_id,
                    RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                )
                .order_by(RequestPaymentAllocation.id)
            )
        )

    def _active_allocated_for_payment(self, payment_id: int) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(
                    RequestPaymentAllocation.group_id == self.group_id,
                    RequestPaymentAllocation.payment_id == payment_id,
                    RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                )
            )
            or 0
        )

    def build(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        as_of: datetime | None = None,
    ) -> ReportProjection:
        require_aware(period_start)
        require_aware(period_end)
        if period_end <= period_start:
            raise ValueError("report period_end must be after period_start")
        as_of = as_of or utc_now()
        require_aware(as_of)

        exclusion_map: dict[tuple[str, str, int], ReportExclusion] = {}

        def exclude(
            code: str,
            target_type: str,
            target_id: int,
            amount_rial: int | None,
            source_message_id: int | None,
        ) -> None:
            key = (code, target_type, target_id)
            exclusion_map.setdefault(
                key,
                ReportExclusion(
                    code=code,
                    target_type=target_type,
                    target_id=target_id,
                    amount_rial=amount_rial,
                    source=self._source(source_message_id),
                ),
            )

        requests = list(
            self.session.scalars(
                select(PaymentRequest)
                .where(
                    PaymentRequest.group_id == self.group_id,
                    PaymentRequest.requested_at >= period_start,
                    PaymentRequest.requested_at < period_end,
                )
                .order_by(PaymentRequest.requested_at, PaymentRequest.id)
            )
        )
        request_lines: list[RequestReportLine] = []
        selected_allocation_ids: set[int] = set()

        for request in requests:
            counterparty = self.session.get(Counterparty, request.counterparty_id)
            counterparty_name = counterparty.display_name if counterparty else f"#{request.counterparty_id}"
            warnings: list[str] = []
            comparable = True
            if request.amount_rial is None:
                comparable = False
                warnings.append("request_amount_or_unit_unknown")
                exclude("unknown_request_amount", "payment_request", request.id, None, request.source_message_id)
            if request.state == RequestState.NEEDS_REVIEW.value:
                comparable = False
                warnings.append("request_needs_review")
            if request.state == RequestState.CANCELLED.value:
                comparable = False
                warnings.append("request_cancelled")

            submitted = 0
            evidence_confirmed = 0
            confirmed = 0
            allocation_ids: list[int] = []
            payment_ids: list[int] = []
            revision_ids: list[int] = []
            request_revision = self._revision_id("payment_request", request.id, request.revision_number)
            if request_revision is not None:
                revision_ids.append(request_revision)

            allocations = self._active_allocations_for_request(request.id)
            request_allocated_total = sum(item.allocated_rial for item in allocations)
            if request.amount_rial is not None and request_allocated_total > request.amount_rial:
                comparable = False
                warnings.append("allocation_total_exceeds_request")
                exclude(
                    "allocation_total_exceeds_request",
                    "payment_request",
                    request.id,
                    request_allocated_total,
                    request.source_message_id,
                )

            for allocation in allocations:
                selected_allocation_ids.add(allocation.id)
                payment = self.session.get(Payment, allocation.payment_id)
                if payment is None or payment.group_id != self.group_id:
                    warnings.append("payment_missing")
                    exclude("payment_missing", "allocation", allocation.id, allocation.allocated_rial, None)
                    continue
                payment_ids.append(payment.id)
                allocation_ids.append(allocation.id)
                allocation_revision = self._revision_id(
                    "allocation", allocation.id, allocation.revision_number
                )
                payment_revision = self._revision_id("payment", payment.id, payment.revision_number)
                revision_ids.extend(
                    item for item in (allocation_revision, payment_revision) if item is not None
                )
                if payment.evidence_state not in COUNTABLE_PAYMENT_STATES:
                    warnings.append(f"payment_{payment.evidence_state}")
                    exclude(
                        f"payment_{payment.evidence_state}",
                        "payment",
                        payment.id,
                        payment.amount_rial,
                        payment.source_message_id,
                    )
                    continue
                if payment.amount_rial is None:
                    warnings.append("payment_amount_or_unit_unknown")
                    exclude("unknown_payment_amount", "payment", payment.id, None, payment.source_message_id)
                    continue
                if self._active_allocated_for_payment(payment.id) > payment.amount_rial:
                    comparable = False
                    warnings.append("allocation_total_exceeds_payment")
                    exclude(
                        "allocation_total_exceeds_payment",
                        "payment",
                        payment.id,
                        payment.amount_rial,
                        payment.source_message_id,
                    )
                    continue
                if payment.counterparty_id is not None and payment.counterparty_id != request.counterparty_id:
                    comparable = False
                    warnings.append("counterparty_mismatch")
                    exclude(
                        "counterparty_mismatch",
                        "allocation",
                        allocation.id,
                        allocation.allocated_rial,
                        payment.source_message_id,
                    )
                    continue
                submitted += allocation.allocated_rial
                if payment.evidence_state == PaymentEvidenceState.CONFIRMED.value:
                    evidence_confirmed += allocation.allocated_rial
                if (
                    payment.evidence_state == PaymentEvidenceState.CONFIRMED.value
                    and allocation.state == AllocationState.CONFIRMED.value
                ):
                    confirmed += allocation.allocated_rial

            unverified = submitted - confirmed
            documented_difference = (
                request.amount_rial - submitted
                if comparable and request.amount_rial is not None
                else None
            )
            confirmed_difference = (
                request.amount_rial - confirmed
                if comparable and request.amount_rial is not None
                else None
            )
            if request.state == RequestState.CANCELLED.value:
                status = "cancelled"
            elif documented_difference is None:
                status = "needs_review"
            elif documented_difference == 0 and unverified == 0:
                status = "complete"
            elif documented_difference == 0:
                status = "awaiting_verification"
            elif documented_difference > 0:
                status = "partial"
            else:
                status = "over_documented"
            request_lines.append(
                RequestReportLine(
                    request_id=request.id,
                    counterparty_id=request.counterparty_id,
                    counterparty_name=counterparty_name,
                    request_state=request.state,
                    requested_rial=request.amount_rial,
                    submitted_rial=submitted,
                    evidence_confirmed_rial=evidence_confirmed,
                    confirmed_rial=confirmed,
                    unverified_rial=unverified,
                    documented_difference_rial=documented_difference,
                    confirmed_difference_rial=confirmed_difference,
                    status=status,
                    allocation_ids=tuple(sorted(set(allocation_ids))),
                    payment_ids=tuple(sorted(set(payment_ids))),
                    revision_ids=tuple(sorted(set(revision_ids))),
                    source=self._source(request.source_message_id),
                    warnings=tuple(dict.fromkeys(warnings)),
                )
            )

        period_payments = list(
            self.session.scalars(
                select(Payment)
                .where(
                    Payment.group_id == self.group_id,
                    Payment.created_at >= period_start,
                    Payment.created_at < period_end,
                )
                .order_by(Payment.created_at, Payment.id)
            )
        )
        unallocated_lines: list[UnallocatedPaymentLine] = []
        for payment in period_payments:
            if payment.evidence_state not in COUNTABLE_PAYMENT_STATES:
                exclude(
                    f"payment_{payment.evidence_state}",
                    "payment",
                    payment.id,
                    payment.amount_rial,
                    payment.source_message_id,
                )
                continue
            if payment.amount_rial is None:
                exclude("unknown_payment_amount", "payment", payment.id, None, payment.source_message_id)
                continue
            allocated = self._active_allocated_for_payment(payment.id)
            if allocated > payment.amount_rial:
                exclude(
                    "allocation_total_exceeds_payment",
                    "payment",
                    payment.id,
                    payment.amount_rial,
                    payment.source_message_id,
                )
                continue
            remaining = payment.amount_rial - allocated
            if remaining <= 0:
                continue
            counterparty = (
                self.session.get(Counterparty, payment.counterparty_id)
                if payment.counterparty_id is not None
                else None
            )
            revision = self._revision_id("payment", payment.id, payment.revision_number)
            unallocated_lines.append(
                UnallocatedPaymentLine(
                    payment_id=payment.id,
                    counterparty_id=payment.counterparty_id,
                    counterparty_name=counterparty.display_name if counterparty else None,
                    amount_rial=payment.amount_rial,
                    unallocated_rial=remaining,
                    evidence_state=payment.evidence_state,
                    revision_ids=(revision,) if revision is not None else (),
                    source=self._source(payment.source_message_id),
                    warnings=("unallocated_payment",),
                )
            )

        promise_rows = list(
            self.session.scalars(
                select(PaymentPromise)
                .where(
                    PaymentPromise.group_id == self.group_id,
                    PaymentPromise.created_at >= period_start,
                    PaymentPromise.created_at < period_end,
                )
                .order_by(PaymentPromise.created_at, PaymentPromise.id)
            )
        )
        promise_lines: list[PromiseReportLine] = []
        for promise in promise_rows:
            counterparty = self.session.get(Counterparty, promise.counterparty_id)
            warnings = () if promise.amount_rial is not None else ("promise_amount_or_unit_unknown",)
            revision = self._revision_id("payment_promise", promise.id, promise.revision_number)
            promise_lines.append(
                PromiseReportLine(
                    promise_id=promise.id,
                    counterparty_id=promise.counterparty_id,
                    counterparty_name=counterparty.display_name if counterparty else f"#{promise.counterparty_id}",
                    amount_rial=promise.amount_rial,
                    state=promise.state,
                    request_id=promise.request_id,
                    revision_ids=(revision,) if revision is not None else (),
                    source=self._source(promise.source_message_id),
                    warnings=warnings,
                )
            )

        exclusions = tuple(sorted(exclusion_map.values(), key=lambda item: (item.code, item.target_type, item.target_id)))
        comparable_lines = [line for line in request_lines if line.documented_difference_rial is not None]
        requested_total = sum(line.requested_rial or 0 for line in comparable_lines)
        line_submitted = sum(line.submitted_rial for line in request_lines)
        unallocated_total = sum(line.unallocated_rial for line in unallocated_lines)
        submitted_total = line_submitted + unallocated_total
        evidence_confirmed_total = sum(
            line.evidence_confirmed_rial for line in request_lines
        ) + sum(
            line.unallocated_rial
            for line in unallocated_lines
            if line.evidence_state == PaymentEvidenceState.CONFIRMED.value
        )
        confirmed_total = sum(line.confirmed_rial for line in request_lines)
        promised_total = sum(
            line.amount_rial or 0
            for line in promise_lines
            if line.state == PromiseState.OPEN.value
        )
        unresolved_count = (
            sum(bool(line.warnings) or line.status != "complete" for line in request_lines)
            + len(unallocated_lines)
            + len(exclusions)
        )
        totals = ReportTotals(
            requested_rial=requested_total,
            submitted_rial=submitted_total,
            evidence_confirmed_rial=evidence_confirmed_total,
            confirmed_rial=confirmed_total,
            unverified_rial=submitted_total - confirmed_total,
            documented_difference_rial=sum(
                line.documented_difference_rial or 0 for line in comparable_lines
            ),
            promised_rial=promised_total,
            excluded_known_rial=sum(item.amount_rial or 0 for item in exclusions),
            comparable_request_count=len(comparable_lines),
            unresolved_count=unresolved_count,
        )

        manifest = {
            "calculation_version": CALCULATION_VERSION,
            "group_id": self.group_id,
            "period_start": _aware_utc(period_start).isoformat(),
            "period_end": _aware_utc(period_end).isoformat(),
            "as_of": _aware_utc(as_of).isoformat(),
            "requests": [
                {
                    "id": line.request_id,
                    "allocations": list(line.allocation_ids),
                    "payments": list(line.payment_ids),
                    "revisions": list(line.revision_ids),
                    "source_message": line.source.message_id if line.source else None,
                }
                for line in request_lines
            ],
            "unallocated_payments": [
                {
                    "id": line.payment_id,
                    "revisions": list(line.revision_ids),
                    "source_message": line.source.message_id if line.source else None,
                }
                for line in unallocated_lines
            ],
            "promises": [
                {
                    "id": line.promise_id,
                    "revisions": list(line.revision_ids),
                    "source_message": line.source.message_id if line.source else None,
                }
                for line in promise_lines
            ],
            "exclusions": [
                {"code": item.code, "type": item.target_type, "id": item.target_id}
                for item in exclusions
            ],
        }
        return ReportProjection(
            group_id=self.group_id,
            group_title=self.group.title,
            period_start=period_start,
            period_end=period_end,
            as_of=as_of,
            calculation_version=CALCULATION_VERSION,
            request_lines=tuple(request_lines),
            unallocated_payments=tuple(unallocated_lines),
            promises=tuple(promise_lines),
            exclusions=exclusions,
            totals=totals,
            manifest=manifest,
            manifest_hash=_canonical_hash(manifest),
        )

    def record_run(
        self,
        report: ReportProjection,
        *,
        requested_by_user_id: int,
        rendered_output: str,
        report_kind: str = "financial_reconciliation",
    ) -> ReportRun:
        if self.session.get(TelegramUser, requested_by_user_id) is None:
            raise LedgerNotFound("requesting user does not exist")
        item = ReportRun(
            group_id=self.group_id,
            report_kind=report_kind,
            period_start=report.period_start,
            period_end=report.period_end,
            as_of=report.as_of,
            requested_by_user_id=requested_by_user_id,
            calculation_version=report.calculation_version,
            filters={"time_basis": "request_requested_at/payment_created_at"},
            manifest_hash=report.manifest_hash,
            output_hash=hashlib.sha256(rendered_output.encode("utf-8")).hexdigest(),
            status="succeeded",
        )
        self.session.add(item)
        self.session.flush()
        return item
