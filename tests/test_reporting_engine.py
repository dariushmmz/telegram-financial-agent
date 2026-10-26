from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ledger_helpers import LedgerSeed
from receipt_bot.domain.enums import (
    CurrencyUnit,
    PaymentEvidenceState,
    RequestState,
    ReviewDecision,
)
from receipt_bot.domain.money import Money
from receipt_bot.persistence.models import ReportRun
from receipt_bot.reconciliation import LedgerService
from receipt_bot.reporting import ReportingEngine, render_persian_report


UTC = timezone.utc
START = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
END = START + timedelta(days=1)


def test_financial_report_exact_partial_unallocated_promise_unknown_and_duplicate(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-6101, title="گزارش", actor_telegram_id=601)
        ledger = LedgerService(session, seed.group.id)
        elnaz = ledger.add_counterparty("الناز")
        amir = ledger.add_counterparty("امیررضا")

        elnaz_request_message = seed.message("درخواست الناز", START + timedelta(hours=1))
        elnaz_receipt_message = seed.message("رسید الناز", START + timedelta(hours=2))
        elnaz_request = ledger.create_request(
            counterparty_id=elnaz.id,
            requested_at=elnaz_request_message.occurred_at,
            money=Money(6_000_000_000, CurrencyUnit.RIAL),
            source_message_id=elnaz_request_message.id,
            actor_user_id=seed.actor.id,
        )
        elnaz_payment = ledger.create_payment(
            money=Money(6_000_000_000, CurrencyUnit.RIAL),
            source_message_id=elnaz_receipt_message.id,
            counterparty_id=elnaz.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
            actor_user_id=seed.actor.id,
        )
        elnaz_allocation = ledger.allocate(
            request_id=elnaz_request.id,
            payment_id=elnaz_payment.id,
            allocated_rial=6_000_000_000,
            actor_user_id=seed.actor.id,
        )
        ledger.review_payment(
            elnaz_payment.id,
            reviewer_user_id=seed.actor.id,
            decision=ReviewDecision.CONFIRMED,
            reason="تأیید سند",
        )
        ledger.confirm_allocation(
            elnaz_allocation.id,
            reviewer_user_id=seed.actor.id,
            reason="تأیید تطبیق",
        )

        amir_request_message = seed.message("درخواست امیر", START + timedelta(hours=3))
        amir_receipt_message = seed.message("رسید امیر", START + timedelta(hours=4))
        amir_request = ledger.create_request(
            counterparty_id=amir.id,
            requested_at=amir_request_message.occurred_at,
            money=Money(11_000_000_000, CurrencyUnit.RIAL),
            source_message_id=amir_request_message.id,
        )
        amir_payment = ledger.create_payment(
            money=Money(10_939_000_000, CurrencyUnit.RIAL),
            source_message_id=amir_receipt_message.id,
            counterparty_id=amir.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        ledger.allocate(
            request_id=amir_request.id,
            payment_id=amir_payment.id,
            allocated_rial=10_939_000_000,
        )

        unallocated_message = seed.message("رسید اضافه", START + timedelta(hours=5))
        ledger.create_payment(
            money=Money(100_000_000, CurrencyUnit.RIAL),
            source_message_id=unallocated_message.id,
            counterparty_id=amir.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        duplicate_message = seed.message("رسید تکراری", START + timedelta(hours=6))
        ledger.create_payment(
            money=Money(100_000_000, CurrencyUnit.RIAL),
            source_message_id=duplicate_message.id,
            counterparty_id=amir.id,
            evidence_state=PaymentEvidenceState.DUPLICATE,
            duplicate_of_payment_id=amir_payment.id,
        )
        unknown_message = seed.message("مبلغ نامشخص", START + timedelta(hours=7))
        ledger.create_payment(
            money=Money(500, CurrencyUnit.UNKNOWN),
            source_message_id=unknown_message.id,
            counterparty_id=amir.id,
        )
        rejected_message = seed.message("رسید ردشده", START + timedelta(hours=7, minutes=30))
        ledger.create_payment(
            money=Money(70_000_000, CurrencyUnit.RIAL),
            source_message_id=rejected_message.id,
            counterparty_id=amir.id,
            evidence_state=PaymentEvidenceState.REJECTED,
        )
        promise_message = seed.message("فردا پرداخت می‌کنم", START + timedelta(hours=8))
        ledger.create_promise(
            counterparty_id=amir.id,
            source_message_id=promise_message.id,
            money=Money(500_000_000, CurrencyUnit.RIAL),
            request_id=amir_request.id,
        )

        unknown_request = ledger.create_request(
            counterparty_id=amir.id,
            requested_at=START + timedelta(hours=9),
            money=Money(123, CurrencyUnit.UNKNOWN),
        )
        cancelled = ledger.create_request(
            counterparty_id=amir.id,
            requested_at=START + timedelta(hours=10),
            money=Money(200_000_000, CurrencyUnit.RIAL),
        )
        ledger.set_request_state(
            cancelled.id,
            state=RequestState.CANCELLED,
            actor_user_id=seed.actor.id,
            reason="لغو درخواست",
        )

        engine = ReportingEngine(session, seed.group.id)
        report = engine.build(period_start=START, period_end=END, as_of=END)
        by_name = {line.counterparty_name: line for line in report.request_lines if line.request_id in {elnaz_request.id, amir_request.id}}
        assert by_name["الناز"].status == "complete"
        assert by_name["الناز"].confirmed_rial == 6_000_000_000
        assert by_name["امیررضا"].status == "partial"
        assert by_name["امیررضا"].documented_difference_rial == 61_000_000
        assert by_name["امیررضا"].confirmed_rial == 0
        assert next(line for line in report.request_lines if line.request_id == unknown_request.id).documented_difference_rial is None
        assert next(line for line in report.request_lines if line.request_id == cancelled.id).status == "cancelled"
        assert len(report.unallocated_payments) == 1
        assert report.unallocated_payments[0].unallocated_rial == 100_000_000
        assert report.totals.requested_rial == 17_000_000_000
        assert report.totals.submitted_rial == 17_039_000_000
        assert report.totals.confirmed_rial == 6_000_000_000
        assert report.totals.unverified_rial == 11_039_000_000
        assert report.totals.documented_difference_rial == 61_000_000
        assert report.totals.promised_rial == 500_000_000
        assert {item.code for item in report.exclusions} >= {
            "payment_duplicate",
            "unknown_payment_amount",
            "unknown_request_amount",
            "payment_rejected",
        }
        assert report.manifest_hash == engine.build(
            period_start=START, period_end=END, as_of=END
        ).manifest_hash

        rendered = render_persian_report(report)
        assert "اختلاف ۶٬۱۰۰٬۰۰۰ تومان" in rendered
        assert "وعده‌های پرداخت (خارج از جمع پرداخت‌ها)" in rendered
        assert "تأیید مستقل وصول بانکی نیستند" in rendered
        assert "بدهی قطعی محسوب نمی‌شوند" in rendered
        run = engine.record_run(
            report,
            requested_by_user_id=seed.actor.id,
            rendered_output=rendered,
        )
        assert session.scalar(select(ReportRun).where(ReportRun.id == run.id)).manifest_hash == report.manifest_hash


def test_split_multiple_receipts_and_overpayment_remainder_are_counted_once(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-6102, title="تقسیم", actor_telegram_id=602)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("مشتری")
        first = ledger.create_request(
            counterparty_id=person.id,
            requested_at=START + timedelta(hours=1),
            money=Money(600, CurrencyUnit.RIAL),
        )
        second = ledger.create_request(
            counterparty_id=person.id,
            requested_at=START + timedelta(hours=1),
            money=Money(400, CurrencyUnit.RIAL),
        )
        payment_message = seed.message("رسید ۱۱۰۰", START + timedelta(hours=2))
        payment = ledger.create_payment(
            money=Money(1_100, CurrencyUnit.RIAL),
            source_message_id=payment_message.id,
            counterparty_id=person.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        ledger.allocate(request_id=first.id, payment_id=payment.id, allocated_rial=600)
        ledger.allocate(request_id=second.id, payment_id=payment.id, allocated_rial=400)

        report = ReportingEngine(session, seed.group.id).build(
            period_start=START, period_end=END, as_of=END
        )
        assert sum(line.submitted_rial for line in report.request_lines) == 1_000
        assert len(report.unallocated_payments) == 1
        assert report.unallocated_payments[0].unallocated_rial == 100
        assert report.totals.submitted_rial == 1_100
        assert report.totals.documented_difference_rial == 0
        payment_occurrences = sum(payment.id in line.payment_ids for line in report.request_lines)
        assert payment_occurrences == 2
        assert sum(line.submitted_rial for line in report.request_lines) + report.unallocated_payments[0].unallocated_rial == payment.amount_rial


def test_multiple_receipts_for_one_request_sum_without_counting_promises(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-6104, title="چند رسید", actor_telegram_id=604)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("مشتری")
        request = ledger.create_request(
            counterparty_id=person.id,
            requested_at=START + timedelta(hours=1),
            money=Money(1_000, CurrencyUnit.RIAL),
        )
        for index, amount in enumerate((300, 700), start=2):
            message = seed.message(f"رسید {index}", START + timedelta(hours=index))
            payment = ledger.create_payment(
                money=Money(amount, CurrencyUnit.RIAL),
                source_message_id=message.id,
                counterparty_id=person.id,
                evidence_state=PaymentEvidenceState.SUBMITTED,
            )
            ledger.allocate(request_id=request.id, payment_id=payment.id, allocated_rial=amount)
        promise_message = seed.message("وعده", START + timedelta(hours=5))
        ledger.create_promise(
            counterparty_id=person.id,
            source_message_id=promise_message.id,
            money=Money(500, CurrencyUnit.RIAL),
            request_id=request.id,
        )
        report = ReportingEngine(session, seed.group.id).build(
            period_start=START, period_end=END, as_of=END
        )
        assert len(report.request_lines[0].payment_ids) == 2
        assert report.request_lines[0].submitted_rial == 1_000
        assert report.request_lines[0].unverified_rial == 1_000
        assert report.request_lines[0].documented_difference_rial == 0
        assert report.totals.submitted_rial == 1_000
        assert report.totals.promised_rial == 500


def test_report_period_is_start_inclusive_end_exclusive(migrated_database) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-6103, title="مرز", actor_telegram_id=603)
        ledger = LedgerService(session, seed.group.id)
        person = ledger.add_counterparty("مرزی")
        included = ledger.create_request(
            counterparty_id=person.id,
            requested_at=START,
            money=Money(100, CurrencyUnit.RIAL),
        )
        ledger.create_request(
            counterparty_id=person.id,
            requested_at=END,
            money=Money(200, CurrencyUnit.RIAL),
        )
        start_message = seed.message("شروع", START)
        end_message = seed.message("پایان", END)
        start_payment = ledger.create_payment(
            money=Money(10, CurrencyUnit.RIAL),
            source_message_id=start_message.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        ledger.create_payment(
            money=Money(20, CurrencyUnit.RIAL),
            source_message_id=end_message.id,
            evidence_state=PaymentEvidenceState.SUBMITTED,
        )
        report = ReportingEngine(session, seed.group.id).build(
            period_start=START, period_end=END, as_of=END
        )
        assert [line.request_id for line in report.request_lines] == [included.id]
        assert [line.payment_id for line in report.unallocated_payments] == [start_payment.id]


def test_confirmed_unallocated_receipt_reports_sender_without_calling_them_payer(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(
            session,
            chat_id=-6105,
            title="هویت فرستنده",
            actor_telegram_id=605,
        )
        message = seed.message("رسید ارسال‌شده", START + timedelta(hours=1))
        payment = LedgerService(session, seed.group.id).create_payment(
            money=Money(1_000, CurrencyUnit.RIAL),
            source_message_id=message.id,
            evidence_state=PaymentEvidenceState.CONFIRMED,
            actor_user_id=seed.actor.id,
        )

        report = ReportingEngine(session, seed.group.id).build(
            period_start=START, period_end=END, as_of=END
        )
        line = report.unallocated_payments[0]
        rendered = render_persian_report(report)

        assert line.payment_id == payment.id
        assert line.counterparty_id is None
        assert line.source is not None
        assert line.source.sender_display_name == "حسابدار"
        assert report.totals.evidence_confirmed_rial == 1_000
        assert report.totals.confirmed_rial == 0
        assert "فرستنده تلگرام: حسابدار (پرداخت‌کننده تعیین نشده)" in rendered
        assert "تأیید حسابداری شده؛ هنوز به درخواست متصل نشده" in rendered
        assert "شخص نامشخص" not in rendered
