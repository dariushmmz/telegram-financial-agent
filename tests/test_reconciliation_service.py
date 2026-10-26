from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from ledger_helpers import LedgerSeed
from receipt_bot.domain.enums import (
    AllocationState,
    CurrencyUnit,
    MatchDisposition,
    PaymentEvidenceState,
    ReviewDecision,
    SettlementState,
)
from receipt_bot.domain.money import Money
from receipt_bot.persistence.models import (
    AuditEvent,
    ExtractionAttempt,
    Payment,
    PaymentEvidence,
    RecordRevision,
    RequestPaymentAllocation,
    Review,
)
from receipt_bot.reconciliation import LedgerConflict, LedgerService, MatchingService


UTC = timezone.utc
NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def test_confirmed_payment_and_allocation_keep_settlement_separate(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5101, title="گروه", actor_telegram_id=501)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("الناز")
        request_message = seed.message("درخواست", NOW)
        payment_message = seed.message("رسید", NOW + timedelta(hours=1))
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(600_000_000, CurrencyUnit.TOMAN),
            source_message_id=request_message.id,
            actor_user_id=seed.actor.id,
        )
        payment = ledger.create_payment(
            money=Money(600_000_000, CurrencyUnit.TOMAN),
            source_message_id=payment_message.id,
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
            actor_user_id=seed.actor.id,
        )
        allocation = ledger.allocate(
            request_id=request.id,
            payment_id=payment.id,
            allocated_rial=6_000_000_000,
            actor_user_id=seed.actor.id,
        )
        ledger.review_payment(
            payment.id,
            reviewer_user_id=seed.actor.id,
            decision=ReviewDecision.CONFIRMED,
            reason="بررسی رسید",
        )
        ledger.confirm_allocation(
            allocation.id,
            reviewer_user_id=seed.actor.id,
            reason="تطبیق با درخواست",
        )
        assert payment.evidence_state == PaymentEvidenceState.CONFIRMED.value
        assert payment.settlement_state == SettlementState.NOT_CHECKED.value
        assert allocation.state == AllocationState.CONFIRMED.value

        ledger.confirm_external_settlement(
            payment.id,
            reviewer_user_id=seed.actor.id,
            reason="تأیید مستقل بانک",
        )
        assert payment.settlement_state == SettlementState.EXTERNALLY_CONFIRMED.value
        assert payment.revision_number == 3
        assert allocation.revision_number == 2
        assert session.scalar(select(func.count()).select_from(Review)) == 2
        assert session.scalar(select(func.count()).select_from(RecordRevision)) == 6


def test_confirmed_allocation_resolves_unassigned_payment_counterparty_with_history(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5109, title="هویت", actor_telegram_id=509)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("الناز")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(100, CurrencyUnit.RIAL),
            actor_user_id=seed.actor.id,
        )
        payment = ledger.create_payment(
            money=Money(100, CurrencyUnit.RIAL),
            evidence_state=PaymentEvidenceState.SUBMITTED,
            actor_user_id=seed.actor.id,
        )
        allocation = ledger.allocate(
            request_id=request.id,
            payment_id=payment.id,
            allocated_rial=100,
            actor_user_id=seed.actor.id,
        )
        ledger.review_payment(
            payment.id,
            reviewer_user_id=seed.actor.id,
            decision=ReviewDecision.CONFIRMED,
            reason="سند معتبر است",
        )
        revision_before = payment.revision_number

        ledger.confirm_allocation(
            allocation.id,
            reviewer_user_id=seed.actor.id,
            reason="پرداخت متعلق به این درخواست است",
        )

        assert payment.counterparty_id == person.id
        assert payment.revision_number == revision_before + 1
        assert allocation.state == AllocationState.CONFIRMED.value
        identity_revision = session.scalar(
            select(RecordRevision).where(
                RecordRevision.target_type == "payment",
                RecordRevision.target_id == payment.id,
                RecordRevision.revision_number == payment.revision_number,
            )
        )
        assert identity_revision is not None
        assert identity_revision.snapshot["counterparty_id"] == person.id


def test_split_allocations_caps_unknown_units_and_counterparty_mismatch(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5102, title="تقسیم", actor_telegram_id=502)
        ledger = LedgerService(session, seed.group.id)
        first_person = ledger.add_counterparty("شخص یک")
        other_person = ledger.add_counterparty("شخص دیگر")
        first = ledger.create_request(
            counterparty_id=first_person.id,
            requested_at=NOW,
            money=Money(600, CurrencyUnit.RIAL),
        )
        second = ledger.create_request(
            counterparty_id=first_person.id,
            requested_at=NOW,
            money=Money(400, CurrencyUnit.RIAL),
        )
        other = ledger.create_request(
            counterparty_id=other_person.id,
            requested_at=NOW,
            money=Money(100, CurrencyUnit.RIAL),
        )
        payment = ledger.create_payment(
            money=Money(1_100, CurrencyUnit.RIAL),
            counterparty_id=first_person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        ledger.allocate(request_id=first.id, payment_id=payment.id, allocated_rial=600)
        ledger.allocate(request_id=second.id, payment_id=payment.id, allocated_rial=400)

        with pytest.raises(LedgerConflict, match="request amount"):
            ledger.allocate(request_id=second.id, payment_id=payment.id, allocated_rial=1)
        with pytest.raises(LedgerConflict, match="counterparties"):
            ledger.allocate(request_id=other.id, payment_id=payment.id, allocated_rial=100)

        unknown = ledger.create_payment(
            money=Money(100, CurrencyUnit.UNKNOWN),
            counterparty_id=first_person.id,
        )
        with pytest.raises(LedgerConflict, match="Unknown|unknown"):
            ledger.allocate(request_id=first.id, payment_id=unknown.id, allocated_rial=1)

        with pytest.raises(LedgerConflict, match="below active allocations"):
            ledger.correct_payment(
                payment.id,
                reviewer_user_id=seed.actor.id,
                reason="مبلغ کمتر",
                money=Money(999, CurrencyUnit.RIAL),
            )

        assert sum(
            session.scalars(
                select(RequestPaymentAllocation.allocated_rial).where(
                    RequestPaymentAllocation.payment_id == payment.id
                )
            )
        ) == 1_000


@pytest.mark.parametrize("parts", [(100,), (30, 70), (1, 2, 3, 94), (25, 25, 25, 25)])
def test_allocation_integer_invariant_for_partitioned_payments(migrated_database, parts) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(
            session,
            chat_id=-5200 - len(parts),
            title="ویژگی",
            actor_telegram_id=5200 + len(parts),
        )
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("شخص")
        payment = ledger.create_payment(
            money=Money(sum(parts), CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        allocations = []
        for value in parts:
            request = ledger.create_request(
                counterparty_id=person.id,
                requested_at=NOW,
                money=Money(value, CurrencyUnit.RIAL),
            )
            allocations.append(
                ledger.allocate(
                    request_id=request.id,
                    payment_id=payment.id,
                    allocated_rial=value,
                )
            )
        assert sum(item.allocated_rial for item in allocations) == payment.amount_rial
        assert all(type(item.allocated_rial) is int for item in allocations)
        with pytest.raises(LedgerConflict):
            extra_request = ledger.create_request(
                counterparty_id=person.id,
                requested_at=NOW,
                money=Money(1, CurrencyUnit.RIAL),
            )
            ledger.allocate(request_id=extra_request.id, payment_id=payment.id, allocated_rial=1)


def test_correction_and_duplicate_review_preserve_history_and_disable_allocations(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5103, title="اصلاح", actor_telegram_id=503)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("امیررضا")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(1_000, CurrencyUnit.RIAL),
        )
        canonical = ledger.create_payment(
            money=Money(1_000, CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        duplicate = ledger.create_payment(
            money=Money(1_000, CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        allocation = ledger.allocate(
            request_id=request.id, payment_id=duplicate.id, allocated_rial=1_000
        )
        ledger.correct_request(
            request.id,
            reviewer_user_id=seed.actor.id,
            reason="اصلاح مبلغ درخواست",
            money=Money(1_100, CurrencyUnit.RIAL),
        )
        ledger.review_payment(
            duplicate.id,
            reviewer_user_id=seed.actor.id,
            decision=ReviewDecision.MARKED_DUPLICATE,
            duplicate_of_payment_id=canonical.id,
            reason="مرجع تکراری",
        )
        assert request.amount_rial == 1_100
        assert request.revision_number == 2
        assert duplicate.evidence_state == PaymentEvidenceState.DUPLICATE.value
        assert duplicate.duplicate_of_payment_id == canonical.id
        assert allocation.state == AllocationState.REJECTED.value
        assert allocation.revision_number == 2
        assert session.scalar(
            select(func.count()).select_from(RecordRevision).where(
                RecordRevision.target_type == "payment_request",
                RecordRevision.target_id == request.id,
            )
        ) == 2
        with pytest.raises(LedgerConflict, match="duplicate"):
            ledger.allocate(request_id=request.id, payment_id=duplicate.id, allocated_rial=1)


def test_matching_returns_unique_explicit_reply_and_ambiguous_amount_candidates(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5104, title="تطبیق", actor_telegram_id=504)
        ledger = LedgerService(session, seed.group.id)
        first_person = ledger.add_counterparty("الف")
        second_person = ledger.add_counterparty("ب")
        first_message = seed.message("درخواست الف", NOW)
        second_message = seed.message("درخواست ب", NOW)
        first = ledger.create_request(
            counterparty_id=first_person.id,
            requested_at=NOW,
            money=Money(500, CurrencyUnit.RIAL),
            source_message_id=first_message.id,
        )
        ledger.create_request(
            counterparty_id=second_person.id,
            requested_at=NOW,
            money=Money(500, CurrencyUnit.RIAL),
            source_message_id=second_message.id,
        )
        ambiguous_message = seed.message("رسید بدون شخص", NOW + timedelta(hours=1))
        ambiguous = ledger.create_payment(
            money=Money(500, CurrencyUnit.RIAL),
            source_message_id=ambiguous_message.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        suggestion = MatchingService(session, seed.group.id).suggest(ambiguous.id)
        assert suggestion.disposition is MatchDisposition.AMBIGUOUS
        assert len(suggestion.candidates) == 2
        assert session.scalar(select(func.count()).select_from(RequestPaymentAllocation)) == 0

        reply_message = seed.message(
            "رسید پاسخ",
            NOW + timedelta(hours=2),
            reply_to_message_id=first_message.telegram_message_id,
        )
        reply_payment = ledger.create_payment(
            money=Money(500, CurrencyUnit.RIAL),
            source_message_id=reply_message.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        reply_suggestion = MatchingService(session, seed.group.id).suggest(reply_payment.id)
        assert reply_suggestion.disposition is MatchDisposition.UNIQUE
        assert reply_suggestion.candidates[0].request_id == first.id
        assert "explicit_reply" in reply_suggestion.candidates[0].reasons


def test_rejected_payment_never_retains_countable_allocation(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5105, title="رد", actor_telegram_id=505)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("شخص")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(100, CurrencyUnit.RIAL),
        )
        payment = ledger.create_payment(
            money=Money(100, CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        allocation = ledger.allocate(request_id=request.id, payment_id=payment.id, allocated_rial=100)
        ledger.review_payment(
            payment.id,
            reviewer_user_id=seed.actor.id,
            decision=ReviewDecision.REJECTED,
            reason="رسید نامعتبر",
        )
        assert payment.evidence_state == PaymentEvidenceState.REJECTED.value
        assert allocation.state == AllocationState.REJECTED.value
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 5


def test_successful_text_extraction_materializes_payment_without_confirming_settlement(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5106, title="متن", actor_telegram_id=506)
        message = seed.message("متن رسید", NOW)
        evidence = PaymentEvidence(
            group_id=seed.group.id,
            source_message_id=message.id,
            evidence_kind="telegram_text",
            content_sha256="a" * 64,
            classification_state="receipt_candidate",
        )
        session.add(evidence)
        session.flush()
        session.add(
            ExtractionAttempt(
                group_id=seed.group.id,
                evidence_id=evidence.id,
                attempt_number=1,
                provider="gemini",
                model="test",
                schema_version="receipt-v3",
                prompt_version="receipt-fa-v3",
                status="succeeded",
                raw_response={},
                normalized_candidate={
                    "amount_value": 667_300,
                    "amount_unit": "rial",
                    "amount_rial": 667_300,
                    "transaction_datetime": "1405/07/21 16:17",
                    "document_number": "14056021324196",
                    "requires_review": False,
                },
                validation_issues=[],
                uncertainty={},
            )
        )
        session.flush()
        ledger = LedgerService(session, seed.group.id)
        assert ledger.materialize_unrecorded_extractions() == 1
        assert ledger.materialize_unrecorded_extractions() == 0
        payment = session.scalar(select(Payment).where(Payment.evidence_id == evidence.id))
        assert payment.amount_rial == 667_300
        assert payment.transaction_reference == "14056021324196"
        assert payment.evidence_state == PaymentEvidenceState.SUBMITTED.value
        assert payment.settlement_state == SettlementState.NOT_CHECKED.value
        assert payment.created_at.replace(tzinfo=UTC) == message.occurred_at


def test_database_guards_reject_bypassed_allocation_invariants(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5107, title="محافظ", actor_telegram_id=507)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("شخص")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(100, CurrencyUnit.RIAL),
        )
        payment = ledger.create_payment(
            money=Money(100, CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        allocation = ledger.allocate(
            request_id=request.id, payment_id=payment.id, allocated_rial=100
        )
        group_id, request_id, payment_id, allocation_id = (
            seed.group.id,
            request.id,
            payment.id,
            allocation.id,
        )

    with pytest.raises(DatabaseError, match="allocation exceeds"):
        with migrated_database.session() as session:
            session.execute(
                text(
                    "INSERT INTO request_payment_allocations "
                    "(group_id, request_id, payment_id, allocated_rial, state, revision_number, created_at, updated_at) "
                    "VALUES (:group_id, :request_id, :payment_id, 1, 'proposed', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"group_id": group_id, "request_id": request_id, "payment_id": payment_id},
            )

    with pytest.raises(DatabaseError, match="payment amount below allocations"):
        with migrated_database.session() as session:
            session.execute(
                text("UPDATE payments SET amount_rial=99 WHERE id=:id"), {"id": payment_id}
            )

    with pytest.raises(DatabaseError, match="payment evidence is not confirmed"):
        with migrated_database.session() as session:
            session.execute(
                text("UPDATE request_payment_allocations SET state='confirmed' WHERE id=:id"),
                {"id": allocation_id},
            )


def test_allocation_and_promise_corrections_append_revisions(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-5108, title="اصلاحات", actor_telegram_id=508)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("شخص")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=NOW,
            money=Money(200, CurrencyUnit.RIAL),
        )
        payment = ledger.create_payment(
            money=Money(200, CurrencyUnit.RIAL),
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        allocation = ledger.allocate(request_id=request.id, payment_id=payment.id, allocated_rial=100)
        message = seed.message("قول پرداخت", NOW)
        promise = ledger.create_promise(
            counterparty_id=person.id,
            source_message_id=message.id,
            money=Money(50, CurrencyUnit.RIAL),
        )
        ledger.correct_allocation_amount(
            allocation.id,
            allocated_rial=150,
            reviewer_user_id=seed.actor.id,
            reason="اصلاح تخصیص",
        )
        ledger.correct_promise(
            promise.id,
            reviewer_user_id=seed.actor.id,
            reason="اصلاح وعده",
            money=Money(75, CurrencyUnit.RIAL),
            expected_at=NOW + timedelta(days=1),
        )
        assert allocation.allocated_rial == 150
        assert allocation.revision_number == 2
        assert promise.amount_rial == 75
        assert promise.revision_number == 2
        corrected_targets = set(
            session.execute(
                select(Review.target_type, Review.target_id).where(
                    Review.decision == ReviewDecision.CORRECTED.value
                )
            )
        )
        assert corrected_targets == {("allocation", allocation.id), ("payment_promise", promise.id)}
