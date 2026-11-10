from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ledger_helpers import LedgerSeed
from receipt_bot.config import AppSettings
from receipt_bot.domain.enums import (
    CurrencyUnit,
    PaymentDateState,
    PaymentEvidenceState,
)
from receipt_bot.domain.money import Money, normalize_digits
from receipt_bot.domain.receipt_date import assess_receipt_date, parse_receipt_datetime
from receipt_bot.domain.time import TEHRAN
from receipt_bot.persistence.models import Payment
from receipt_bot.reconciliation import LedgerConflict, LedgerService
from receipt_bot.reporting import ReportingEngine, render_persian_report
from receipt_bot.telegram.financial import FinancialWorkflowService, Period
from receipt_bot.telegram.handler import TelegramUpdateHandler
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway
from test_telegram_financial_workflows import (
    activate,
    click,
    complete_prompt,
    find_button,
    run_command,
    seed_finance,
)


NOW = datetime(2026, 11, 4, 10, 30, tzinfo=TEHRAN)


def test_jalali_gregorian_named_dates_and_invalid_leap_days() -> None:
    numeric = parse_receipt_datetime("۱۴۰۵/۰۸/۱۳ ۱۰:۳۰")
    named = parse_receipt_datetime("۱۰:۳۰ چهارشنبه، ۱۳ آبان ۱۴۰۵")
    gregorian = parse_receipt_datetime("2026-11-04 10:30:00")
    assert numeric and named and gregorian
    assert numeric.calendar == named.calendar == "jalali"
    assert numeric.value == named.value == gregorian.value
    assert parse_receipt_datetime("۱۳۹۹/۱۲/۳۰").value.date().isoformat() == "2021-03-20"
    assert parse_receipt_datetime("۱۴۰۰/۱۲/۳۰") is None
    assert parse_receipt_datetime("2026-02-30") is None


def test_tehran_calendar_boundary_future_and_unknown_are_deterministic() -> None:
    exact = assess_receipt_date("2026/10/05 23:59", now=NOW, max_age_days=30)
    old = assess_receipt_date("2026/10/04", now=NOW, max_age_days=30)
    future = assess_receipt_date("2026/11/05", now=NOW, max_age_days=30)
    unknown = assess_receipt_date("تاریخ ناخوانا", now=NOW, max_age_days=30)
    assert exact.age_days == 30 and exact.state is PaymentDateState.VALID
    assert old.age_days == 31 and old.state is PaymentDateState.NEEDS_REVIEW_OLD
    assert future.age_days == -1 and future.state is PaymentDateState.NEEDS_REVIEW_FUTURE
    assert unknown.parsed is None and unknown.state is PaymentDateState.NEEDS_REVIEW_UNKNOWN

    just_before_midnight = datetime(2026, 11, 4, 23, 59, tzinfo=TEHRAN)
    just_after_midnight = just_before_midnight + timedelta(minutes=2)
    assert assess_receipt_date("2026/10/05", now=just_before_midnight, max_age_days=30).state is PaymentDateState.VALID
    assert assess_receipt_date("2026/10/05", now=just_after_midnight, max_age_days=30).state is PaymentDateState.NEEDS_REVIEW_OLD


def test_date_policy_default_and_override(tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{(tmp_path / 'date.db').as_posix()}",
        "data_root": tmp_path / "data",
        "allowed_telegram_usernames": "operator_test",
        "_env_file": None,
    }
    assert AppSettings(**base).receipt_max_age_days == 30
    assert AppSettings(**base, receipt_max_age_days=45).receipt_max_age_days == 45
    with pytest.raises(ValueError):
        AppSettings(**base, receipt_max_age_days=-1)


def test_old_payment_is_visible_excluded_then_approval_and_rejection_are_audited(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-7101, title="تاریخ رسید", actor_telegram_id=701)
        ledger = LedgerService(
            session,
            seed.group.id,
            receipt_max_age_days=30,
            now_factory=lambda: NOW,
        )
        party = ledger.add_counterparty("مشتری")
        request = ledger.create_request(
            counterparty_id=party.id,
            requested_at=NOW.astimezone(UTC),
            money=Money(1_000_000, CurrencyUnit.RIAL),
            actor_user_id=seed.actor.id,
        )
        message = seed.message("رسید", NOW.astimezone(UTC))
        payment = ledger.create_payment(
            money=Money(1_000_000, CurrencyUnit.RIAL),
            source_message_id=message.id,
            counterparty_id=party.id,
            printed_transaction_at="2026/10/04 12:00",
            evidence_state=PaymentEvidenceState.SUBMITTED,
            actor_user_id=seed.actor.id,
            recorded_at=NOW.astimezone(UTC),
            assess_date=True,
        )
        assert payment.printed_transaction_at == "2026/10/04 12:00"
        assert payment.date_eligibility_state == PaymentDateState.NEEDS_REVIEW_OLD.value
        with pytest.raises(LedgerConflict, match="date requires"):
            ledger.allocate(
                request_id=request.id,
                payment_id=payment.id,
                allocated_rial=1_000_000,
                actor_user_id=seed.actor.id,
            )

        start = NOW.astimezone(UTC) - timedelta(hours=1)
        end = start + timedelta(days=1)
        report = ReportingEngine(session, seed.group.id).build(
            period_start=start, period_end=end, as_of=end
        )
        assert report.totals.submitted_rial == 0
        assert "payment_date_needs_review_old" in {item.code for item in report.exclusions}
        assert "تا تأیید حسابدار" in render_persian_report(report)

        revision_before = payment.revision_number
        ledger.approve_payment_date(
            payment.id,
            reviewer_user_id=seed.actor.id,
            reason="اصل رسید بررسی شد",
        )
        assert payment.evidence_state == PaymentEvidenceState.SUBMITTED.value
        assert payment.date_eligibility_state == PaymentDateState.APPROVED_EXCEPTION.value
        assert payment.revision_number == revision_before + 1
        allocation = ledger.allocate(
            request_id=request.id,
            payment_id=payment.id,
            allocated_rial=1_000_000,
            actor_user_id=seed.actor.id,
        )
        report = ReportingEngine(session, seed.group.id).build(
            period_start=start, period_end=end, as_of=end
        )
        assert report.totals.submitted_rial == 1_000_000

        ledger.reject_payment_date(
            payment.id,
            reviewer_user_id=seed.actor.id,
            reason="تاریخ قابل پذیرش نیست",
        )
        rejected_revision = payment.revision_number
        ledger.reject_payment_date(
            payment.id,
            reviewer_user_id=seed.actor.id,
            reason="تکرار همان تصمیم",
        )
        assert payment.revision_number == rejected_revision
        assert allocation.state == "proposed"
        report = ReportingEngine(session, seed.group.id).build(
            period_start=start, period_end=end, as_of=end
        )
        assert report.totals.submitted_rial == 0
        assert report.totals.confirmed_rial == 0

    finance = FinancialWorkflowService(migrated_database)
    period = Period(NOW.date(), 1)
    receipts, *_ = finance.receipts(seed.group.id, period)
    pending, *_ = finance.pending(seed.group.id)
    assert "تاریخ ردشده" in receipts
    assert "تاریخ ردشده" in pending  # evidence review remains separate from date rejection


def test_date_correction_preserves_original_and_duplicate_never_becomes_countable(
    migrated_database,
) -> None:
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-7102, title="اصلاح تاریخ", actor_telegram_id=702)
        ledger = LedgerService(
            session,
            seed.group.id,
            receipt_max_age_days=30,
            now_factory=lambda: NOW,
        )
        canonical = ledger.create_payment(
            money=Money(500, CurrencyUnit.RIAL),
            printed_transaction_at="2026/11/04",
            assess_date=True,
            recorded_at=NOW.astimezone(UTC),
        )
        duplicate = ledger.create_payment(
            money=Money(500, CurrencyUnit.RIAL),
            printed_transaction_at="تاریخ ناخوانا",
            evidence_state=PaymentEvidenceState.DUPLICATE,
            duplicate_of_payment_id=canonical.id,
            assess_date=True,
            recorded_at=NOW.astimezone(UTC),
        )
        assert duplicate.date_eligibility_state == PaymentDateState.NEEDS_REVIEW_UNKNOWN.value
        ledger.correct_payment_date(
            duplicate.id,
            corrected_transaction_at="۱۴۰۵/۰۸/۱۳ ۱۰:۳۰",
            reviewer_user_id=seed.actor.id,
            reason="خواندن مجدد تصویر",
        )
        assert duplicate.printed_transaction_at == "تاریخ ناخوانا"
        assert duplicate.corrected_transaction_at == "۱۴۰۵/۰۸/۱۳ ۱۰:۳۰"
        assert duplicate.date_eligibility_state == PaymentDateState.VALID.value
        start = NOW.astimezone(UTC) - timedelta(hours=1)
        report = ReportingEngine(session, seed.group.id).build(
            period_start=start,
            period_end=start + timedelta(days=1),
            as_of=start + timedelta(days=1),
        )
        assert report.totals.submitted_rial == 500
        assert "payment_duplicate" in {item.code for item in report.exclusions}


def test_verify_date_exception_requires_reason_and_does_not_confirm_evidence(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database,
        gateway,
        access_policy=TEST_ACCESS_POLICY,
        receipt_max_age_days=30,
    )
    chat_id = -1007103
    activate(handler, gateway, chat_id=chat_id)
    seeded = seed_finance(migrated_database, chat_id=chat_id)
    with migrated_database.session() as session:
        payment = session.get(Payment, seeded["payment_id"])
        actor_id = seeded["actor_id"]
        ledger = LedgerService(
            session,
            seeded["group_id"],
            receipt_max_age_days=30,
            now_factory=lambda: NOW,
        )
        ledger.correct_payment_date(
            payment.id,
            corrected_transaction_at="2026/10/04",
            reviewer_user_id=actor_id,
            reason="آماده‌سازی سناریوی تست",
        )

    pending = run_command(
        handler, gateway, update_id=2, chat_id=chat_id, command="/verify"
    )
    payment_button = find_button(pending["reply_markup"], "رسید #")["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=payment_button)
    approve = find_button(detail["reply_markup"], "تأیید استثنای تاریخ")["callback_data"]
    prompt = click(handler, gateway, update_id=4, chat_id=chat_id, data=approve)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="اصل رسید و علت تأخیر بررسی شد",
        update_id=5,
    )
    result = click(handler, gateway, update_id=6, chat_id=chat_id, data=final)
    assert "فقط مانع تاریخ" in result["text"]

    with migrated_database.session() as session:
        payment = session.get(Payment, seeded["payment_id"])
        assert payment.date_eligibility_state == PaymentDateState.APPROVED_EXCEPTION.value
        assert payment.evidence_state == PaymentEvidenceState.SUBMITTED.value


def test_suspicious_date_is_visible_but_excluded_consistently_across_financial_views(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database,
        gateway,
        access_policy=TEST_ACCESS_POLICY,
        receipt_max_age_days=30,
    )
    chat_id = -1007104
    activate(handler, gateway, chat_id=chat_id)
    seeded = seed_finance(migrated_database, chat_id=chat_id)

    with migrated_database.session() as session:
        ledger = LedgerService(
            session,
            seeded["group_id"],
            receipt_max_age_days=30,
            now_factory=lambda: NOW,
        )
        ledger.correct_payment_date(
            seeded["payment_id"],
            corrected_transaction_at="2026/10/04",
            reviewer_user_id=seeded["actor_id"],
            reason="سناریوی یکپارچه تاریخ قدیمی",
        )

    period = Period.today()
    finance = FinancialWorkflowService(migrated_database)
    report = finance.report(seeded["group_id"], seeded["actor_id"], period)
    summary = finance.summary(seeded["group_id"], period)
    receipts, payment_ids, *_ = finance.receipts(seeded["group_id"], period)
    pending, pending_payment_ids, *_ = finance.pending(seeded["group_id"])
    transactions, *_ = finance.transactions(seeded["group_id"], period)
    balance = finance.balance(seeded["group_id"], seeded["party_id"])

    assert seeded["payment_id"] in payment_ids
    assert seeded["payment_id"] in pending_payment_ids
    assert "تاریخ قدیمی" in receipts
    assert "تاریخ قدیمی؛ تا تأیید حسابدار از جمع‌ها خارج است" in pending
    assert f"رسید #{seeded['payment_id']}" in normalize_digits(transactions)
    for rendered in (report, summary):
        assert "تاریخ قدیمی، آینده یا نامعتبر" in rendered
    assert "۰ تومان" in report
    assert "به‌دلیل وضعیت تاریخ در این مانده محاسبه نشده" in balance
