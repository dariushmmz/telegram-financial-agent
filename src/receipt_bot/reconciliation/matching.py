from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from receipt_bot.domain.enums import AllocationState, MatchDisposition, RequestState
from receipt_bot.persistence.models import (
    Counterparty,
    Payment,
    PaymentRequest,
    RequestPaymentAllocation,
    TelegramMessage,
)
from receipt_bot.reconciliation.service import LedgerNotFound


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    request_id: int
    counterparty_id: int
    counterparty_name: str
    requested_rial: int | None
    remaining_rial: int | None
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchSuggestion:
    payment_id: int
    disposition: MatchDisposition
    candidates: tuple[MatchCandidate, ...]
    warning: str | None = None


class MatchingService:
    """Produces deterministic suggestions only; it never creates allocations."""

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id

    def _payment(self, payment_id: int) -> Payment:
        payment = self.session.scalar(
            select(Payment).where(Payment.id == payment_id, Payment.group_id == self.group_id)
        )
        if payment is None:
            raise LedgerNotFound("payment does not belong to this group")
        return payment

    def _remaining(self, request: PaymentRequest) -> int | None:
        if request.amount_rial is None:
            return None
        allocated = int(
            self.session.scalar(
                select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(
                    RequestPaymentAllocation.group_id == self.group_id,
                    RequestPaymentAllocation.request_id == request.id,
                    RequestPaymentAllocation.state.in_(
                        [AllocationState.PROPOSED.value, AllocationState.CONFIRMED.value]
                    ),
                )
            )
            or 0
        )
        return request.amount_rial - allocated

    def _candidate(self, request: PaymentRequest, payment: Payment, *, reply_match: bool) -> MatchCandidate | None:
        counterparty = self.session.get(Counterparty, request.counterparty_id)
        if counterparty is None or counterparty.group_id != self.group_id:
            return None
        remaining = self._remaining(request)
        reasons: list[str] = []
        score = 0
        if reply_match:
            reasons.append("explicit_reply")
            score += 100
        if payment.counterparty_id is not None and payment.counterparty_id == request.counterparty_id:
            reasons.append("same_counterparty")
            score += 5
        if payment.amount_rial is not None and remaining is not None:
            if payment.amount_rial == remaining:
                reasons.append("exact_remaining_amount")
                score += 5
            elif 0 < payment.amount_rial <= remaining:
                reasons.append("compatible_partial_amount")
                score += 2
        if not reasons:
            return None
        return MatchCandidate(
            request_id=request.id,
            counterparty_id=request.counterparty_id,
            counterparty_name=counterparty.display_name,
            requested_rial=request.amount_rial,
            remaining_rial=remaining,
            score=score,
            reasons=tuple(reasons),
        )

    def suggest(
        self,
        payment_id: int,
        *,
        explicit_request_id: int | None = None,
        lookback_days: int = 90,
    ) -> MatchSuggestion:
        payment = self._payment(payment_id)
        if explicit_request_id is not None:
            request = self.session.scalar(
                select(PaymentRequest).where(
                    PaymentRequest.id == explicit_request_id,
                    PaymentRequest.group_id == self.group_id,
                )
            )
            if request is None:
                raise LedgerNotFound("explicit request does not belong to this group")
            if request.state in {RequestState.CANCELLED.value, RequestState.CLOSED.value}:
                return MatchSuggestion(payment.id, MatchDisposition.NONE, (), "request_not_open")
            candidate = self._candidate(request, payment, reply_match=True)
            return MatchSuggestion(
                payment.id,
                MatchDisposition.UNIQUE if candidate else MatchDisposition.NONE,
                (candidate,) if candidate else (),
                None if candidate else "explicit_request_incompatible",
            )

        reply_message_id: int | None = None
        if payment.source_message_id is not None:
            source = self.session.get(TelegramMessage, payment.source_message_id)
            if source is not None and source.group_id == self.group_id:
                reply_message_id = source.reply_to_message_id

        created_at = payment.created_at
        start = created_at - timedelta(days=lookback_days)
        end = created_at + timedelta(days=1)
        requests = list(
            self.session.scalars(
                select(PaymentRequest)
                .where(
                    PaymentRequest.group_id == self.group_id,
                    PaymentRequest.state.not_in(
                        [RequestState.CANCELLED.value, RequestState.CLOSED.value]
                    ),
                    PaymentRequest.requested_at >= start,
                    PaymentRequest.requested_at < end,
                )
                .order_by(PaymentRequest.requested_at, PaymentRequest.id)
            )
        )
        candidates: list[MatchCandidate] = []
        for request in requests:
            reply_match = False
            if reply_message_id is not None and request.source_message_id is not None:
                source = self.session.get(TelegramMessage, request.source_message_id)
                reply_match = bool(
                    source is not None
                    and source.group_id == self.group_id
                    and source.telegram_message_id == reply_message_id
                )
            candidate = self._candidate(request, payment, reply_match=reply_match)
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(key=lambda item: (-item.score, item.request_id))
        explicit_reply = [item for item in candidates if "explicit_reply" in item.reasons]
        if len(explicit_reply) == 1:
            return MatchSuggestion(payment.id, MatchDisposition.UNIQUE, tuple(explicit_reply))
        if not candidates:
            return MatchSuggestion(payment.id, MatchDisposition.NONE, (), "no_deterministic_candidate")
        if len(candidates) == 1:
            return MatchSuggestion(payment.id, MatchDisposition.UNIQUE, tuple(candidates))
        return MatchSuggestion(
            payment.id,
            MatchDisposition.AMBIGUOUS,
            tuple(candidates),
            "multiple_candidates_require_accountant_selection",
        )
