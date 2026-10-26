from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from receipt_bot.domain.enums import (
    AllocationState,
    CurrencyUnit,
    PaymentEvidenceState,
    PromiseState,
    RequestState,
    ReviewDecision,
    SettlementState,
)
from receipt_bot.domain.money import Money
from receipt_bot.domain.time import require_aware
from receipt_bot.persistence.models import (
    Counterparty,
    ExtractionAttempt,
    Payment,
    PaymentEvidence,
    PaymentPromise,
    PaymentRequest,
    RecordRevision,
    RequestPaymentAllocation,
    Review,
    TelegramMessage,
    TelegramUser,
)
from receipt_bot.persistence.repositories import HistoryRepository

ACTIVE_ALLOCATION_STATES = {
    AllocationState.PROPOSED.value,
    AllocationState.CONFIRMED.value,
}


class LedgerError(ValueError):
    pass


class LedgerNotFound(LedgerError):
    pass


class LedgerConflict(LedgerError):
    pass


def _money_columns(money: Money | None) -> tuple[int | None, str, int | None]:
    if money is None:
        return None, CurrencyUnit.UNKNOWN.value, None
    return money.value, money.unit.value, money.canonical_rial


def _snapshot(item: Any) -> dict[str, Any]:
    excluded = {"created_at", "updated_at"}
    result: dict[str, Any] = {}
    for column in item.__table__.columns:
        if column.name in excluded:
            continue
        value = getattr(item, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result


class LedgerService:
    """Group-scoped deterministic ledger mutations within the caller's transaction."""

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id
        self.history = HistoryRepository(session, group_id)

    def _get(self, model, item_id: int):
        item = self.session.scalar(
            select(model).where(model.id == item_id, model.group_id == self.group_id)
        )
        if item is None:
            raise LedgerNotFound(f"{model.__name__} does not belong to this group")
        return item

    def _source_message(self, source_message_id: int | None) -> TelegramMessage | None:
        if source_message_id is None:
            return None
        return self._get(TelegramMessage, source_message_id)

    def _actor(self, actor_user_id: int | None) -> TelegramUser | None:
        if actor_user_id is None:
            return None
        actor = self.session.get(TelegramUser, actor_user_id)
        if actor is None:
            raise LedgerNotFound("actor does not exist")
        return actor

    def _append_initial(
        self,
        target_type: str,
        item: Any,
        actor_user_id: int | None,
        *,
        reason: str = "initial",
    ) -> RecordRevision:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise LedgerConflict("an initial record reason is required")
        revision = self.history.append_revision(
            target_type=target_type,
            target_id=item.id,
            revision_number=1,
            snapshot=_snapshot(item),
            reason=normalized_reason,
            actor_user_id=actor_user_id,
        )
        self.history.append_audit(
            action=f"{target_type}.created",
            target_type=target_type,
            target_id=item.id,
            metadata={"revision": 1},
            actor_user_id=actor_user_id,
        )
        return revision

    def _latest_revision(self, target_type: str, target_id: int) -> RecordRevision:
        revision = self.session.scalar(
            select(RecordRevision)
            .where(
                RecordRevision.group_id == self.group_id,
                RecordRevision.target_type == target_type,
                RecordRevision.target_id == target_id,
            )
            .order_by(RecordRevision.revision_number.desc())
            .limit(1)
        )
        if revision is None:
            raise LedgerConflict("record has no initial revision")
        return revision

    def _append_change(
        self,
        *,
        target_type: str,
        item: Any,
        reason: str,
        actor_user_id: int,
        decision: ReviewDecision | None = None,
        comment: str | None = None,
    ) -> RecordRevision:
        if not reason.strip():
            raise LedgerConflict("a correction or state change requires a reason")
        before = self._latest_revision(target_type, item.id)
        revision_number = int(item.revision_number)
        after = self.history.append_revision(
            target_type=target_type,
            target_id=item.id,
            revision_number=revision_number,
            snapshot=_snapshot(item),
            reason=reason.strip(),
            actor_user_id=actor_user_id,
        )
        if decision is not None:
            self.session.add(
                Review(
                    group_id=self.group_id,
                    target_type=target_type,
                    target_id=item.id,
                    reviewer_user_id=actor_user_id,
                    decision=decision.value,
                    comment=comment,
                    before_revision_id=before.id,
                    after_revision_id=after.id,
                )
            )
        self.history.append_audit(
            action=f"{target_type}.{decision.value if decision else 'changed'}",
            target_type=target_type,
            target_id=item.id,
            metadata={"before_revision": before.revision_number, "after_revision": revision_number},
            actor_user_id=actor_user_id,
        )
        self.session.flush()
        return after

    def add_counterparty(
        self,
        display_name: str,
        *,
        aliases: list[str] | None = None,
        telegram_user_id: int | None = None,
    ) -> Counterparty:
        name = " ".join(display_name.split())
        if not name:
            raise LedgerConflict("counterparty name is required")
        item = Counterparty(
            group_id=self.group_id,
            display_name=name,
            normalized_aliases=sorted({" ".join(alias.split()) for alias in aliases or [] if alias.strip()}),
            telegram_user_id=telegram_user_id,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def create_request(
        self,
        *,
        counterparty_id: int,
        requested_at: datetime,
        money: Money | None,
        source_message_id: int | None = None,
        due_at: datetime | None = None,
        actor_user_id: int | None = None,
        state: RequestState = RequestState.OPEN,
        reason: str = "initial",
    ) -> PaymentRequest:
        self._get(Counterparty, counterparty_id)
        self._source_message(source_message_id)
        self._actor(actor_user_id)
        require_aware(requested_at)
        if due_at is not None:
            require_aware(due_at)
        amount_value, amount_unit, amount_rial = _money_columns(money)
        item = PaymentRequest(
            group_id=self.group_id,
            counterparty_id=counterparty_id,
            source_message_id=source_message_id,
            amount_value=amount_value,
            amount_unit=amount_unit,
            amount_rial=amount_rial,
            requested_at=requested_at,
            due_at=due_at,
            state=state.value,
            created_by_user_id=actor_user_id,
            revision_number=1,
        )
        self.session.add(item)
        self.session.flush()
        self._append_initial("payment_request", item, actor_user_id, reason=reason)
        return item

    def create_payment(
        self,
        *,
        money: Money | None,
        evidence_id: int | None = None,
        source_message_id: int | None = None,
        counterparty_id: int | None = None,
        printed_transaction_at: str | None = None,
        transaction_reference: str | None = None,
        evidence_state: PaymentEvidenceState = PaymentEvidenceState.NEEDS_REVIEW,
        duplicate_of_payment_id: int | None = None,
        actor_user_id: int | None = None,
        recorded_at: datetime | None = None,
    ) -> Payment:
        self._actor(actor_user_id)
        receipt_id: int | None = None
        if evidence_id is not None:
            evidence = self._get(PaymentEvidence, evidence_id)
            existing = self.session.scalar(select(Payment.id).where(Payment.evidence_id == evidence.id))
            if existing is not None:
                raise LedgerConflict("payment evidence already has a payment")
            source_message_id = evidence.source_message_id
            receipt_id = evidence.receipt_image_id
        source_message = self._source_message(source_message_id)
        if counterparty_id is not None:
            self._get(Counterparty, counterparty_id)
        if duplicate_of_payment_id is not None:
            self._get(Payment, duplicate_of_payment_id)
        if evidence_state is PaymentEvidenceState.DUPLICATE and duplicate_of_payment_id is None:
            raise LedgerConflict("duplicate payment requires a canonical payment")
        if evidence_state is not PaymentEvidenceState.DUPLICATE and duplicate_of_payment_id is not None:
            raise LedgerConflict("canonical duplicate link is only valid for duplicate payments")
        amount_value, amount_unit, amount_rial = _money_columns(money)
        item = Payment(
            group_id=self.group_id,
            evidence_id=evidence_id,
            duplicate_of_payment_id=duplicate_of_payment_id,
            receipt_id=receipt_id,
            source_message_id=source_message_id,
            counterparty_id=counterparty_id,
            amount_value=amount_value,
            amount_unit=amount_unit,
            amount_rial=amount_rial,
            printed_transaction_at=printed_transaction_at,
            transaction_reference=transaction_reference,
            evidence_state=evidence_state.value,
            settlement_state=SettlementState.NOT_CHECKED.value,
            revision_number=1,
        )
        if recorded_at is not None:
            require_aware(recorded_at)
            item.created_at = recorded_at
            item.updated_at = recorded_at
        elif source_message is not None:
            item.created_at = source_message.occurred_at
            item.updated_at = source_message.occurred_at
        self.session.add(item)
        self.session.flush()
        self._append_initial("payment", item, actor_user_id)
        return item

    def create_payment_from_evidence(
        self,
        evidence_id: int,
        *,
        actor_user_id: int | None = None,
        recorded_at: datetime | None = None,
    ) -> Payment:
        evidence = self._get(PaymentEvidence, evidence_id)
        attempt = self.session.scalar(
            select(ExtractionAttempt)
            .where(
                ExtractionAttempt.group_id == self.group_id,
                ExtractionAttempt.evidence_id == evidence.id,
                ExtractionAttempt.status == "succeeded",
            )
            .order_by(ExtractionAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is None or not attempt.normalized_candidate:
            raise LedgerConflict("evidence has no successful normalized extraction")
        candidate = attempt.normalized_candidate
        try:
            unit = CurrencyUnit(str(candidate.get("amount_unit", CurrencyUnit.UNKNOWN.value)))
        except ValueError:
            unit = CurrencyUnit.UNKNOWN
        value = candidate.get("amount_value")
        money = Money(int(value), unit) if isinstance(value, int) and value >= 0 else None
        reference = next(
            (
                str(candidate[key])
                for key in ("bank_reference", "reference_number", "document_number", "tracking_code")
                if candidate.get(key)
            ),
            None,
        )
        duplicate_payment: Payment | None = None
        if evidence.duplicate_disposition == "exact" and evidence.duplicate_of_evidence_id:
            duplicate_payment = self.session.scalar(
                select(Payment).where(
                    Payment.group_id == self.group_id,
                    Payment.evidence_id == evidence.duplicate_of_evidence_id,
                )
            )
            if duplicate_payment is None:
                raise LedgerConflict("canonical duplicate evidence must become a payment first")
        state = (
            PaymentEvidenceState.DUPLICATE
            if duplicate_payment is not None
            else PaymentEvidenceState.NEEDS_REVIEW
            if candidate.get("requires_review", True)
            else PaymentEvidenceState.SUBMITTED
        )
        return self.create_payment(
            money=money,
            evidence_id=evidence.id,
            printed_transaction_at=candidate.get("transaction_datetime"),
            transaction_reference=reference,
            evidence_state=state,
            duplicate_of_payment_id=duplicate_payment.id if duplicate_payment else None,
            actor_user_id=actor_user_id,
            recorded_at=recorded_at,
        )

    def materialize_unrecorded_extractions(self) -> int:
        """Create payment candidates for successful extractions missed before Step 04."""
        evidence_ids = list(
            self.session.scalars(
                select(PaymentEvidence.id)
                .join(ExtractionAttempt, ExtractionAttempt.evidence_id == PaymentEvidence.id)
                .outerjoin(Payment, Payment.evidence_id == PaymentEvidence.id)
                .where(
                    PaymentEvidence.group_id == self.group_id,
                    ExtractionAttempt.group_id == self.group_id,
                    ExtractionAttempt.status == "succeeded",
                    Payment.id.is_(None),
                )
                .distinct()
                .order_by(PaymentEvidence.duplicate_of_evidence_id.is_not(None), PaymentEvidence.id)
            )
        )
        created = 0
        for evidence_id in evidence_ids:
            try:
                self.create_payment_from_evidence(evidence_id)
            except LedgerConflict as exc:
                if "canonical duplicate evidence" not in str(exc):
                    raise
            else:
                created += 1
        return created

    def create_promise(
        self,
        *,
        counterparty_id: int,
        source_message_id: int,
        money: Money | None,
        request_id: int | None = None,
        expected_at: datetime | None = None,
        actor_user_id: int | None = None,
        reason: str = "initial",
    ) -> PaymentPromise:
        self._get(Counterparty, counterparty_id)
        source_message = self._source_message(source_message_id)
        self._actor(actor_user_id)
        if request_id is not None:
            request = self._get(PaymentRequest, request_id)
            if request.counterparty_id != counterparty_id:
                raise LedgerConflict("promise and request counterparties differ")
        if expected_at is not None:
            require_aware(expected_at)
        amount_value, amount_unit, amount_rial = _money_columns(money)
        item = PaymentPromise(
            group_id=self.group_id,
            counterparty_id=counterparty_id,
            source_message_id=source_message_id,
            request_id=request_id,
            amount_value=amount_value,
            amount_unit=amount_unit,
            amount_rial=amount_rial,
            expected_at=expected_at,
            state=PromiseState.OPEN.value,
            revision_number=1,
        )
        self.session.add(item)
        item.created_at = source_message.occurred_at
        item.updated_at = source_message.occurred_at
        self.session.flush()
        self._append_initial("payment_promise", item, actor_user_id, reason=reason)
        return item

    def _allocation_total_for_payment(self, payment_id: int, *, exclude_id: int | None = None) -> int:
        conditions = [
            RequestPaymentAllocation.group_id == self.group_id,
            RequestPaymentAllocation.payment_id == payment_id,
            RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
        ]
        if exclude_id is not None:
            conditions.append(RequestPaymentAllocation.id != exclude_id)
        return int(self.session.scalar(select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(*conditions)) or 0)

    def _allocation_total_for_request(self, request_id: int, *, exclude_id: int | None = None) -> int:
        conditions = [
            RequestPaymentAllocation.group_id == self.group_id,
            RequestPaymentAllocation.request_id == request_id,
            RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
        ]
        if exclude_id is not None:
            conditions.append(RequestPaymentAllocation.id != exclude_id)
        return int(self.session.scalar(select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(*conditions)) or 0)

    def allocate(
        self,
        *,
        request_id: int,
        payment_id: int,
        allocated_rial: int,
        actor_user_id: int | None = None,
        state: AllocationState = AllocationState.PROPOSED,
    ) -> RequestPaymentAllocation:
        self._actor(actor_user_id)
        request = self._get(PaymentRequest, request_id)
        payment = self._get(Payment, payment_id)
        if isinstance(allocated_rial, bool) or not isinstance(allocated_rial, int) or allocated_rial <= 0:
            raise LedgerConflict("allocated amount must be a positive integer rial value")
        if request.state in {RequestState.CANCELLED.value, RequestState.CLOSED.value}:
            raise LedgerConflict("cannot allocate to a closed or cancelled request")
        if payment.evidence_state in {PaymentEvidenceState.DUPLICATE.value, PaymentEvidenceState.REJECTED.value}:
            raise LedgerConflict("duplicate or rejected payment cannot be allocated")
        if request.amount_rial is None or payment.amount_rial is None:
            raise LedgerConflict("unknown-unit amounts cannot be allocated")
        if payment.counterparty_id is not None and payment.counterparty_id != request.counterparty_id:
            raise LedgerConflict("payment and request counterparties differ")
        if state is AllocationState.CONFIRMED and payment.evidence_state != PaymentEvidenceState.CONFIRMED.value:
            raise LedgerConflict("allocation cannot be confirmed before payment evidence")
        if self._allocation_total_for_payment(payment.id) + allocated_rial > payment.amount_rial:
            raise LedgerConflict("allocation total exceeds payment amount")
        if self._allocation_total_for_request(request.id) + allocated_rial > request.amount_rial:
            raise LedgerConflict("allocation total exceeds request amount")
        item = RequestPaymentAllocation(
            group_id=self.group_id,
            request_id=request.id,
            payment_id=payment.id,
            allocated_rial=allocated_rial,
            state=state.value,
            revision_number=1,
            created_by_user_id=actor_user_id,
        )
        self.session.add(item)
        self.session.flush()
        self._append_initial("allocation", item, actor_user_id)
        return item

    def correct_payment(
        self,
        payment_id: int,
        *,
        reviewer_user_id: int,
        reason: str,
        money: Money | None = None,
        counterparty_id: int | None = None,
        printed_transaction_at: str | None = None,
        transaction_reference: str | None = None,
    ) -> Payment:
        self._actor(reviewer_user_id)
        payment = self._get(Payment, payment_id)
        if counterparty_id is not None:
            self._get(Counterparty, counterparty_id)
        if money is not None:
            value, unit, rial = _money_columns(money)
            active_allocated = self._allocation_total_for_payment(payment.id)
            if active_allocated and rial is None:
                raise LedgerConflict("payment with active allocations cannot change to an unknown unit")
            if rial is not None and active_allocated > rial:
                raise LedgerConflict("corrected payment amount is below active allocations")
            payment.amount_value, payment.amount_unit, payment.amount_rial = value, unit, rial
        if counterparty_id is not None:
            allocated_request_counterparties = set(
                self.session.scalars(
                    select(PaymentRequest.counterparty_id)
                    .join(RequestPaymentAllocation, RequestPaymentAllocation.request_id == PaymentRequest.id)
                    .where(
                        RequestPaymentAllocation.group_id == self.group_id,
                        RequestPaymentAllocation.payment_id == payment.id,
                        RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                    )
                )
            )
            if allocated_request_counterparties and allocated_request_counterparties != {counterparty_id}:
                raise LedgerConflict("corrected counterparty conflicts with active allocations")
            payment.counterparty_id = counterparty_id
        if printed_transaction_at is not None:
            payment.printed_transaction_at = printed_transaction_at
        if transaction_reference is not None:
            payment.transaction_reference = transaction_reference
        payment.evidence_state = PaymentEvidenceState.CONFIRMED.value
        payment.duplicate_of_payment_id = None
        payment.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment",
            item=payment,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CORRECTED,
            comment=reason,
        )
        return payment

    def assign_payment_counterparty(
        self,
        payment_id: int,
        *,
        counterparty_id: int,
        reviewer_user_id: int,
        reason: str,
    ) -> Payment:
        """Resolve a payer manually without changing evidence or settlement state."""
        self._actor(reviewer_user_id)
        payment = self._get(Payment, payment_id)
        self._get(Counterparty, counterparty_id)
        allocated_request_counterparties = set(
            self.session.scalars(
                select(PaymentRequest.counterparty_id)
                .join(
                    RequestPaymentAllocation,
                    RequestPaymentAllocation.request_id == PaymentRequest.id,
                )
                .where(
                    RequestPaymentAllocation.group_id == self.group_id,
                    RequestPaymentAllocation.payment_id == payment.id,
                    RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                )
            )
        )
        if allocated_request_counterparties and allocated_request_counterparties != {
            counterparty_id
        }:
            raise LedgerConflict("counterparty conflicts with active allocations")
        if payment.counterparty_id == counterparty_id:
            raise LedgerConflict("payment already has this counterparty")
        payment.counterparty_id = counterparty_id
        payment.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment",
            item=payment,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CORRECTED,
            comment=reason,
        )
        return payment

    def correct_request(
        self,
        request_id: int,
        *,
        reviewer_user_id: int,
        reason: str,
        money: Money | None = None,
        counterparty_id: int | None = None,
    ) -> PaymentRequest:
        self._actor(reviewer_user_id)
        request = self._get(PaymentRequest, request_id)
        if counterparty_id is not None:
            self._get(Counterparty, counterparty_id)
        if money is not None:
            value, unit, rial = _money_columns(money)
            active_allocated = self._allocation_total_for_request(request.id)
            if active_allocated and rial is None:
                raise LedgerConflict("request with active allocations cannot change to an unknown unit")
            if rial is not None and active_allocated > rial:
                raise LedgerConflict("corrected request amount is below active allocations")
            request.amount_value, request.amount_unit, request.amount_rial = value, unit, rial
        if counterparty_id is not None:
            allocated_payment_counterparties = set(
                self.session.scalars(
                    select(Payment.counterparty_id)
                    .join(RequestPaymentAllocation, RequestPaymentAllocation.payment_id == Payment.id)
                    .where(
                        RequestPaymentAllocation.group_id == self.group_id,
                        RequestPaymentAllocation.request_id == request.id,
                        RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                        Payment.counterparty_id.is_not(None),
                    )
                )
            )
            if allocated_payment_counterparties and allocated_payment_counterparties != {counterparty_id}:
                raise LedgerConflict("corrected counterparty conflicts with active allocations")
            request.counterparty_id = counterparty_id
        request.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment_request",
            item=request,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CORRECTED,
            comment=reason,
        )
        return request

    def review_payment(
        self,
        payment_id: int,
        *,
        reviewer_user_id: int,
        decision: ReviewDecision,
        reason: str,
        duplicate_of_payment_id: int | None = None,
    ) -> Payment:
        self._actor(reviewer_user_id)
        payment = self._get(Payment, payment_id)
        next_state: str
        next_duplicate_id: int | None = None
        if decision is ReviewDecision.CONFIRMED:
            next_state = PaymentEvidenceState.CONFIRMED.value
        elif decision is ReviewDecision.REJECTED:
            next_state = PaymentEvidenceState.REJECTED.value
        elif decision is ReviewDecision.MARKED_DUPLICATE:
            if duplicate_of_payment_id is None:
                raise LedgerConflict("canonical payment is required")
            canonical = self._get(Payment, duplicate_of_payment_id)
            if canonical.id == payment.id or canonical.evidence_state == PaymentEvidenceState.DUPLICATE.value:
                raise LedgerConflict("invalid canonical payment")
            next_state = PaymentEvidenceState.DUPLICATE.value
            next_duplicate_id = canonical.id
        elif decision is ReviewDecision.REOPENED:
            next_state = PaymentEvidenceState.NEEDS_REVIEW.value
        else:
            raise LedgerConflict("use correct_payment for correction decisions")

        if decision in {ReviewDecision.REJECTED, ReviewDecision.MARKED_DUPLICATE}:
            allocations = list(
                self.session.scalars(
                    select(RequestPaymentAllocation).where(
                        RequestPaymentAllocation.group_id == self.group_id,
                        RequestPaymentAllocation.payment_id == payment.id,
                        RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                    )
                )
            )
            for allocation in allocations:
                allocation.state = AllocationState.REJECTED.value
                allocation.revision_number += 1
                self.session.flush()
                self._append_change(
                    target_type="allocation",
                    item=allocation,
                    reason=f"payment {decision.value}: {reason}",
                    actor_user_id=reviewer_user_id,
                )
        payment.evidence_state = next_state
        payment.duplicate_of_payment_id = next_duplicate_id
        payment.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment",
            item=payment,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=decision,
            comment=reason,
        )
        return payment

    def confirm_allocation(
        self,
        allocation_id: int,
        *,
        reviewer_user_id: int,
        reason: str,
    ) -> RequestPaymentAllocation:
        self._actor(reviewer_user_id)
        allocation = self._get(RequestPaymentAllocation, allocation_id)
        payment = self._get(Payment, allocation.payment_id)
        request = self._get(PaymentRequest, allocation.request_id)
        if allocation.state != AllocationState.PROPOSED.value:
            raise LedgerConflict("only proposed allocations can be confirmed")
        if payment.evidence_state != PaymentEvidenceState.CONFIRMED.value:
            raise LedgerConflict("payment evidence must be confirmed first")
        if payment.counterparty_id not in {None, request.counterparty_id}:
            raise LedgerConflict("payment and request counterparties differ")
        if payment.counterparty_id is None:
            payment.counterparty_id = request.counterparty_id
            payment.revision_number += 1
            self.session.flush()
            self._append_change(
                target_type="payment",
                item=payment,
                reason=f"identity resolved by confirmed allocation #{allocation.id}: {reason}",
                actor_user_id=reviewer_user_id,
            )
        allocation.state = AllocationState.CONFIRMED.value
        allocation.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="allocation",
            item=allocation,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CONFIRMED,
            comment=reason,
        )
        return allocation

    def correct_allocation_amount(
        self,
        allocation_id: int,
        *,
        allocated_rial: int,
        reviewer_user_id: int,
        reason: str,
    ) -> RequestPaymentAllocation:
        self._actor(reviewer_user_id)
        allocation = self._get(RequestPaymentAllocation, allocation_id)
        if allocation.state not in ACTIVE_ALLOCATION_STATES:
            raise LedgerConflict("inactive allocation cannot be corrected")
        if isinstance(allocated_rial, bool) or not isinstance(allocated_rial, int) or allocated_rial <= 0:
            raise LedgerConflict("allocated amount must be a positive integer rial value")
        request = self._get(PaymentRequest, allocation.request_id)
        payment = self._get(Payment, allocation.payment_id)
        if request.amount_rial is None or payment.amount_rial is None:
            raise LedgerConflict("unknown-unit amounts cannot be allocated")
        if self._allocation_total_for_request(request.id, exclude_id=allocation.id) + allocated_rial > request.amount_rial:
            raise LedgerConflict("allocation total exceeds request amount")
        if self._allocation_total_for_payment(payment.id, exclude_id=allocation.id) + allocated_rial > payment.amount_rial:
            raise LedgerConflict("allocation total exceeds payment amount")
        allocation.allocated_rial = allocated_rial
        allocation.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="allocation",
            item=allocation,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CORRECTED,
            comment=reason,
        )
        return allocation

    def cancel_allocation(
        self,
        allocation_id: int,
        *,
        actor_user_id: int,
        reason: str,
    ) -> RequestPaymentAllocation:
        self._actor(actor_user_id)
        allocation = self._get(RequestPaymentAllocation, allocation_id)
        if allocation.state not in ACTIVE_ALLOCATION_STATES:
            raise LedgerConflict("allocation is already inactive")
        allocation.state = AllocationState.CANCELLED.value
        allocation.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="allocation",
            item=allocation,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        return allocation

    def set_request_state(
        self,
        request_id: int,
        *,
        state: RequestState,
        actor_user_id: int,
        reason: str,
    ) -> PaymentRequest:
        self._actor(actor_user_id)
        request = self._get(PaymentRequest, request_id)
        if state in {RequestState.CANCELLED, RequestState.CLOSED} and self._allocation_total_for_request(request.id):
            raise LedgerConflict("cancel active allocations before closing or cancelling the request")
        request.state = state.value
        request.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment_request",
            item=request,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        return request

    def set_promise_state(
        self,
        promise_id: int,
        *,
        state: PromiseState,
        actor_user_id: int,
        reason: str,
    ) -> PaymentPromise:
        self._actor(actor_user_id)
        promise = self._get(PaymentPromise, promise_id)
        promise.state = state.value
        promise.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment_promise",
            item=promise,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        return promise

    def correct_promise(
        self,
        promise_id: int,
        *,
        reviewer_user_id: int,
        reason: str,
        money: Money | None = None,
        expected_at: datetime | None = None,
    ) -> PaymentPromise:
        self._actor(reviewer_user_id)
        promise = self._get(PaymentPromise, promise_id)
        if money is not None:
            value, unit, rial = _money_columns(money)
            promise.amount_value, promise.amount_unit, promise.amount_rial = value, unit, rial
        if expected_at is not None:
            require_aware(expected_at)
            promise.expected_at = expected_at
        promise.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment_promise",
            item=promise,
            reason=reason,
            actor_user_id=reviewer_user_id,
            decision=ReviewDecision.CORRECTED,
            comment=reason,
        )
        return promise

    def confirm_external_settlement(
        self,
        payment_id: int,
        *,
        reviewer_user_id: int,
        reason: str,
    ) -> Payment:
        self._actor(reviewer_user_id)
        payment = self._get(Payment, payment_id)
        if payment.evidence_state != PaymentEvidenceState.CONFIRMED.value:
            raise LedgerConflict("accounting evidence must be confirmed before external settlement")
        payment.settlement_state = SettlementState.EXTERNALLY_CONFIRMED.value
        payment.revision_number += 1
        self.session.flush()
        self._append_change(
            target_type="payment",
            item=payment,
            reason=reason,
            actor_user_id=reviewer_user_id,
        )
        return payment
