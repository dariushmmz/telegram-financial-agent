from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway

from receipt_bot.domain.enums import CurrencyUnit, PaymentEvidenceState
from receipt_bot.domain.money import Money
from receipt_bot.domain.time import utc_now
from receipt_bot.persistence.models import (
    Counterparty,
    Payment,
    PaymentPromise,
    PaymentRequest,
    RecordRevision,
    ReportRun,
    RequestPaymentAllocation,
    Review,
    TelegramGroup,
    TelegramUser,
)
from receipt_bot.persistence.repositories import (
    MembershipRepository,
    MessageRepository,
    UserRepository,
)
from receipt_bot.reconciliation import LedgerService
from receipt_bot.telegram import texts
from receipt_bot.telegram.handler import TelegramUpdateHandler


def user(user_id: int, name: str = "کاربر") -> dict:
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": name,
        "username": f"user_{user_id}",
    }


def group_message(
    update_id: int,
    message_id: int,
    *,
    chat_id: int,
    actor: dict,
    text: str,
    reply_to_message_id: int | None = None,
) -> dict:
    message = {
        "message_id": message_id,
        "date": int(utc_now().timestamp()),
        "chat": {"id": chat_id, "type": "supergroup", "title": f"گروه {chat_id}"},
        "from": actor,
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id, "message": message}


def callback_update(
    update_id: int,
    *,
    chat_id: int,
    actor: dict,
    data: str,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": actor,
            "data": data,
            "message": {
                "message_id": 90_000 + update_id,
                "date": int(utc_now().timestamp()),
                "chat": {"id": chat_id, "type": "supergroup", "title": f"گروه {chat_id}"},
            },
        },
    }


def private_start(update_id: int, user_id: int, token: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(utc_now().timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": user(user_id, "مدیر"),
            "text": f"/start {token}",
        },
    }


def find_button(markup: dict, prefix: str) -> dict:
    return next(
        button
        for row in markup["inline_keyboard"]
        for button in row
        if button["text"].startswith(prefix)
    )


def activate(handler, gateway, *, chat_id: int, actor_id: int = 10) -> None:
    gateway.set_member(chat_id, actor_id, "administrator")
    asyncio.run(
        handler.handle(
            group_message(
                1,
                1,
                chat_id=chat_id,
                actor=user(actor_id, "مدیر"),
                text="/start",
            )
        )
    )


def seed_finance(
    database,
    *,
    chat_id: int,
    two_payments: bool = False,
    payment_has_counterparty: bool = True,
):
    now = utc_now()
    with database.session() as session:
        group = session.scalar(select(TelegramGroup).where(TelegramGroup.telegram_chat_id == chat_id))
        actor = session.scalar(select(TelegramUser).where(TelegramUser.telegram_user_id == 10))
        message, _ = MessageRepository(session).add_idempotent(
            group_id=group.id,
            telegram_message_id=501,
            telegram_update_id=90_501,
            occurred_at=now,
            message_type="text",
            sender_user_id=actor.id,
            text="درخواست پرداخت نمونه",
        )
        receipt_message, _ = MessageRepository(session).add_idempotent(
            group_id=group.id,
            telegram_message_id=502,
            telegram_update_id=90_502,
            occurred_at=now,
            message_type="text",
            sender_user_id=actor.id,
            text="رسید نمونه",
        )
        ledger = LedgerService(session, group.id)
        party = ledger.add_counterparty("الناز")
        request = ledger.create_request(
            counterparty_id=party.id,
            requested_at=now,
            money=Money(6_000_000, CurrencyUnit.TOMAN),
            source_message_id=message.id,
            actor_user_id=actor.id,
        )
        payment = ledger.create_payment(
            money=Money(60_000_000, CurrencyUnit.RIAL),
            source_message_id=receipt_message.id,
            counterparty_id=party.id if payment_has_counterparty else None,
            transaction_reference="SENSITIVE-REF-7788",
            printed_transaction_at="1405/07/29 12:30",
            evidence_state=PaymentEvidenceState.SUBMITTED,
            actor_user_id=actor.id,
            recorded_at=now,
        )
        second = None
        if two_payments:
            second = ledger.create_payment(
                money=Money(60_000_000, CurrencyUnit.RIAL),
                source_message_id=receipt_message.id,
                counterparty_id=party.id if payment_has_counterparty else None,
                transaction_reference="SECOND-REF",
                evidence_state=PaymentEvidenceState.SUBMITTED,
                actor_user_id=actor.id,
                recorded_at=now + timedelta(seconds=1),
            )
        return {
            "group_id": group.id,
            "actor_id": actor.id,
            "party_id": party.id,
            "request_id": request.id,
            "payment_id": payment.id,
            "second_payment_id": second.id if second else None,
        }


def run_command(handler, gateway, *, update_id: int, chat_id: int, command: str, actor_id: int = 10):
    asyncio.run(
        handler.handle(
            group_message(
                update_id,
                update_id,
                chat_id=chat_id,
                actor=user(actor_id, "مدیر" if actor_id == 10 else "کاربر"),
                text=command,
            )
        )
    )
    return gateway.sent[-1]


def click(handler, gateway, *, update_id: int, chat_id: int, data: str, actor_id: int = 10):
    asyncio.run(
        handler.handle(
            callback_update(
                update_id,
                chat_id=chat_id,
                actor=user(actor_id, "مدیر" if actor_id == 10 else "کاربر"),
                data=data,
            )
        )
    )
    return gateway.sent[-1]


def complete_prompt(
    handler,
    gateway,
    *,
    chat_id: int,
    prompt_message_id: int | None,
    text: str,
    update_id: int,
):
    asyncio.run(
        handler.handle(
            group_message(
                update_id,
                update_id,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text=text,
                reply_to_message_id=prompt_message_id,
            )
        )
    )
    return find_button(gateway.sent[-1]["reply_markup"], "تأیید نهایی")["callback_data"]


@pytest.mark.parametrize(
    "command",
    [
        "/reports",
        "/summary",
        "/pending",
        "/verify",
        "/receipts",
        "/transactions",
        "/balance",
        "/request",
        "/promise",
    ],
)
def test_financial_commands_reject_unauthorized_members(migrated_database, command) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    activate(handler, gateway, chat_id=-1005101)
    gateway.set_member(-1005101, 20, "member")

    sent = run_command(
        handler,
        gateway,
        update_id=100 + len(gateway.sent),
        chat_id=-1005101,
        command=command,
        actor_id=20,
    )

    assert sent["text"] == texts.UNAUTHORIZED


def test_reports_default_today_record_run_and_private_source_trace(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database,
        gateway,
        access_policy=TEST_ACCESS_POLICY,
        bot_username="receipt_test_bot",
    )
    chat_id = -1005201
    activate(handler, gateway, chat_id=chat_id)
    seed_finance(migrated_database, chat_id=chat_id)

    sent = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/reports")

    assert sent["text"].startswith("گزارش حساب‌ها")
    assert "رسیدها به معنی تأیید مستقل وصول بانکی نیستند" in sent["text"]
    assert "SENSITIVE-REF-7788" not in sent["text"]
    private_button = find_button(sent["reply_markup"], "منابع گزارش در خصوصی")
    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ReportRun)) == 1

    previous = find_button(sent["reply_markup"], "روز قبل")["callback_data"]
    click(handler, gateway, update_id=3, chat_id=chat_id, data=previous)
    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ReportRun)) == 2

    token = parse_qs(urlparse(private_button["url"]).query)["start"][0]
    asyncio.run(handler.handle(private_start(4, 10, token)))
    assert "منابع گزارش مجاز" in gateway.sent[-1]["text"]
    assert "https://t.me/c/5201/501" in gateway.sent[-1]["text"]

    invalid = run_command(
        handler,
        gateway,
        update_id=5,
        chat_id=chat_id,
        command="/reports ۱۴۰۵/۰۷/۲۹",
    )
    assert "YYYY-MM-DD" in invalid["text"]


def test_receipts_keep_sensitive_fields_private(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database,
        gateway,
        access_policy=TEST_ACCESS_POLICY,
        bot_username="receipt_test_bot",
    )
    chat_id = -1005301
    activate(handler, gateway, chat_id=chat_id)
    seed_finance(migrated_database, chat_id=chat_id)

    sent = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/receipts")
    assert "SENSITIVE-REF-7788" not in sent["text"]
    detail = find_button(sent["reply_markup"], "جزئیات خصوصی")
    token = parse_qs(urlparse(detail["url"]).query)["start"][0]

    asyncio.run(handler.handle(private_start(3, 10, token)))

    assert "SENSITIVE-REF-7788" in gateway.sent[-1]["text"]
    assert "https://t.me/c/5301/502" in gateway.sent[-1]["text"]
    assert asyncio.run(handler.handle(private_start(4, 10, token))) is True
    assert gateway.sent[-1]["text"] == texts.PRIVATE_HANDOFF_INVALID


def test_summary_transactions_and_balance_workflows(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005401
    activate(handler, gateway, chat_id=chat_id)
    seed_finance(migrated_database, chat_id=chat_id)

    summary = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/summary")
    assert "خلاصه مالی" in summary["text"]
    assert "تأییدنشده" in summary["text"]
    transactions = run_command(
        handler, gateway, update_id=3, chat_id=chat_id, command="/transactions"
    )
    assert "درخواست #" in transactions["text"]
    assert "رسید #" in transactions["text"]

    balance_picker = run_command(
        handler, gateway, update_id=4, chat_id=chat_id, command="/balance"
    )
    party = find_button(balance_picker["reply_markup"], "شخص #")["callback_data"]
    balance = click(handler, gateway, update_id=5, chat_id=chat_id, data=party)
    assert "وضعیت ثبت‌شده برای الناز" in balance["text"]
    assert "بدهی قطعی یا مانده بانکی شخص نیست" in balance["text"]


def test_verify_confirm_and_allocation_are_final_confirmed_and_idempotent(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005501
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    with migrated_database.session() as session:
        LedgerService(session, seed["group_id"]).allocate(
            request_id=seed["request_id"],
            payment_id=seed["payment_id"],
            allocated_rial=60_000_000,
            actor_user_id=seed["actor_id"],
        )

    pending = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/verify")
    payment_button = find_button(pending["reply_markup"], f"رسید #{seed['payment_id']}")["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=payment_button)
    confirm = find_button(detail["reply_markup"], "تأیید سند")["callback_data"]
    click(handler, gateway, update_id=4, chat_id=chat_id, data=confirm)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="تطبیق مبلغ و مرجع توسط حسابدار",
        update_id=5,
    )
    click(handler, gateway, update_id=6, chat_id=chat_id, data=final)
    click(handler, gateway, update_id=7, chat_id=chat_id, data=final)
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED

    pending = run_command(handler, gateway, update_id=8, chat_id=chat_id, command="/pending")
    allocation = find_button(pending["reply_markup"], "تخصیص #")["callback_data"]
    allocation_detail = click(handler, gateway, update_id=9, chat_id=chat_id, data=allocation)
    confirm_allocation = find_button(allocation_detail["reply_markup"], "تأیید تخصیص")["callback_data"]
    click(handler, gateway, update_id=10, chat_id=chat_id, data=confirm_allocation)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="تخصیص با درخواست مطابقت دارد",
        update_id=11,
    )
    click(handler, gateway, update_id=12, chat_id=chat_id, data=final)

    transactions = run_command(
        handler, gateway, update_id=13, chat_id=chat_id, command="/transactions"
    )
    review_payment = find_button(transactions["reply_markup"], "بررسی رسید #")["callback_data"]
    detail = click(handler, gateway, update_id=14, chat_id=chat_id, data=review_payment)
    settlement = find_button(detail["reply_markup"], "ثبت وصول مستقل")["callback_data"]
    click(handler, gateway, update_id=15, chat_id=chat_id, data=settlement)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="وصول از مسیر مستقل بانکی بررسی شد",
        update_id=16,
    )
    click(handler, gateway, update_id=17, chat_id=chat_id, data=final)

    with migrated_database.session() as session:
        payment = session.get(Payment, seed["payment_id"])
        allocation_row = session.scalar(select(RequestPaymentAllocation))
        assert payment.evidence_state == "confirmed"
        assert payment.settlement_state == "externally_confirmed"
        assert allocation_row.state == "confirmed"
        assert session.scalar(select(func.count()).select_from(Review)) == 2


def test_confirmed_unlinked_receipt_is_consistent_and_identity_can_be_resolved(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    chat_id = -1005511
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(
        migrated_database,
        chat_id=chat_id,
        payment_has_counterparty=False,
    )

    pending = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/verify")
    payment_button = find_button(
        pending["reply_markup"], f"رسید #{seed['payment_id']}"
    )["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=payment_button)
    assert "پرداخت‌کننده: تعیین‌نشده" in detail["text"]
    assert "فرستنده تلگرام: مدیر" in detail["text"]
    confirm = find_button(detail["reply_markup"], "تأیید سند")["callback_data"]
    click(handler, gateway, update_id=4, chat_id=chat_id, data=confirm)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="رسید از نظر حسابداری بررسی شد",
        update_id=5,
    )
    click(handler, gateway, update_id=6, chat_id=chat_id, data=final)

    pending_after = run_command(
        handler, gateway, update_id=7, chat_id=chat_id, command="/pending"
    )
    assert "مورد بازی برای بررسی وجود ندارد" in pending_after["text"]
    receipts = run_command(
        handler, gateway, update_id=8, chat_id=chat_id, command="/receipts"
    )
    assert "فرستنده تلگرام مدیر؛ پرداخت‌کننده تعیین‌نشده" in receipts["text"]
    assert "تأیید حسابداری" in receipts["text"]
    report = run_command(
        handler, gateway, update_id=9, chat_id=chat_id, command="/reports"
    )
    assert "فرستنده تلگرام: مدیر (پرداخت‌کننده تعیین نشده)" in report["text"]
    assert "تأیید حسابداری شده؛ هنوز به درخواست متصل نشده" in report["text"]
    transactions = run_command(
        handler, gateway, update_id=10, chat_id=chat_id, command="/transactions"
    )
    assert "فرستنده تلگرام مدیر؛ پرداخت‌کننده تعیین‌نشده" in transactions["text"]
    review_payment = find_button(
        transactions["reply_markup"], f"بررسی رسید #{seed['payment_id']}"
    )["callback_data"]
    detail = click(handler, gateway, update_id=11, chat_id=chat_id, data=review_payment)
    identity = find_button(detail["reply_markup"], "تعیین پرداخت‌کننده")["callback_data"]
    picker = click(handler, gateway, update_id=12, chat_id=chat_id, data=identity)
    choose_party = find_button(picker["reply_markup"], "الناز")["callback_data"]
    click(handler, gateway, update_id=13, chat_id=chat_id, data=choose_party)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="هویت پرداخت‌کننده با مدارک بررسی شد",
        update_id=14,
    )
    click(handler, gateway, update_id=15, chat_id=chat_id, data=final)

    with migrated_database.session() as session:
        payment = session.get(Payment, seed["payment_id"])
        assert payment.counterparty_id == seed["party_id"]
        assert payment.evidence_state == PaymentEvidenceState.CONFIRMED.value
    report_after = run_command(
        handler, gateway, update_id=16, chat_id=chat_id, command="/reports"
    )
    assert "• الناز:" in report_after["text"]
    assert "شخص نامشخص" not in report_after["text"]


def test_correction_preserves_original_revision(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005601
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)

    pending = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/verify")
    payment_button = find_button(pending["reply_markup"], "رسید #")["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=payment_button)
    correction = find_button(detail["reply_markup"], "اصلاح مبلغ")["callback_data"]
    click(handler, gateway, update_id=4, chat_id=chat_id, data=correction)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="۷٬۰۰۰٬۰۰۰ تومان | مبلغ روی رسید دوباره خوانده شد",
        update_id=5,
    )
    click(handler, gateway, update_id=6, chat_id=chat_id, data=final)

    with migrated_database.session() as session:
        payment = session.get(Payment, seed["payment_id"])
        revisions = list(
            session.scalars(
                select(RecordRevision)
                .where(RecordRevision.target_type == "payment", RecordRevision.target_id == payment.id)
                .order_by(RecordRevision.revision_number)
            )
        )
        assert payment.amount_rial == 70_000_000
        assert payment.evidence_state == "confirmed"
        assert [item.revision_number for item in revisions] == [1, 2]
        assert revisions[0].snapshot["amount_rial"] == 60_000_000


def test_duplicate_workflow_requires_canonical_and_reason(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005701
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id, two_payments=True)

    pending = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/verify")
    second = find_button(pending["reply_markup"], f"رسید #{seed['second_payment_id']}")["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=second)
    duplicate = find_button(detail["reply_markup"], "تکراری")["callback_data"]
    candidates = click(handler, gateway, update_id=4, chat_id=chat_id, data=duplicate)
    canonical = find_button(candidates["reply_markup"], f"رسید اصلی #{seed['payment_id']}")["callback_data"]
    click(handler, gateway, update_id=5, chat_id=chat_id, data=canonical)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="تصویر دوم همان تراکنش قبلی است",
        update_id=6,
    )
    click(handler, gateway, update_id=7, chat_id=chat_id, data=final)

    with migrated_database.session() as session:
        duplicate_payment = session.get(Payment, seed["second_payment_id"])
        assert duplicate_payment.evidence_state == "duplicate"
        assert duplicate_payment.duplicate_of_payment_id == seed["payment_id"]


def test_link_workflow_supports_split_allocations_without_overcounting(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005801
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    now = utc_now()
    with migrated_database.session() as session:
        ledger = LedgerService(session, seed["group_id"])
        ledger.correct_request(
            seed["request_id"],
            reviewer_user_id=seed["actor_id"],
            reason="تقسیم درخواست",
            money=Money(3_000_000, CurrencyUnit.TOMAN),
        )
        second_request = ledger.create_request(
            counterparty_id=seed["party_id"],
            requested_at=now,
            money=Money(3_000_000, CurrencyUnit.TOMAN),
            actor_user_id=seed["actor_id"],
        )
        second_request_id = second_request.id

    update_id = 2
    for request_id in (seed["request_id"], second_request_id):
        pending = run_command(handler, gateway, update_id=update_id, chat_id=chat_id, command="/verify")
        update_id += 1
        payment_button = find_button(pending["reply_markup"], "رسید #")["callback_data"]
        detail = click(handler, gateway, update_id=update_id, chat_id=chat_id, data=payment_button)
        update_id += 1
        link = find_button(detail["reply_markup"], "اتصال/تقسیم")["callback_data"]
        requests = click(handler, gateway, update_id=update_id, chat_id=chat_id, data=link)
        update_id += 1
        selected = find_button(requests["reply_markup"], f"درخواست #{request_id}")["callback_data"]
        click(handler, gateway, update_id=update_id, chat_id=chat_id, data=selected)
        update_id += 1
        final = complete_prompt(
            handler,
            gateway,
            chat_id=chat_id,
            prompt_message_id=len(gateway.sent),
            text="۳٬۰۰۰٬۰۰۰ تومان | تخصیص بخشی از رسید",
            update_id=update_id,
        )
        update_id += 1
        click(handler, gateway, update_id=update_id, chat_id=chat_id, data=final)
        update_id += 1

    with migrated_database.session() as session:
        allocations = list(session.scalars(select(RequestPaymentAllocation)))
        assert len(allocations) == 2
        assert sum(item.allocated_rial for item in allocations) == 60_000_000
        assert {item.request_id for item in allocations} == {seed["request_id"], second_request_id}


def test_reauthorization_blocks_revoked_reviewer_before_final_action(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1005901
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    gateway.set_member(chat_id, 20, "member")
    with migrated_database.session() as session:
        reviewer, _ = UserRepository(session).get_or_create(20, "حسابدار", None)
        MembershipRepository(session, seed["group_id"]).set_accountant(
            user_id=reviewer.id,
            active=True,
            actor_user_id=seed["actor_id"],
        )

    pending = run_command(
        handler, gateway, update_id=2, chat_id=chat_id, command="/verify", actor_id=20
    )
    payment_button = find_button(pending["reply_markup"], "رسید #")["callback_data"]
    detail = click(
        handler, gateway, update_id=3, chat_id=chat_id, data=payment_button, actor_id=20
    )
    confirm = find_button(detail["reply_markup"], "تأیید سند")["callback_data"]
    gateway.set_member(chat_id, 20, "left")
    click(handler, gateway, update_id=4, chat_id=chat_id, data=confirm, actor_id=20)

    assert gateway.sent[-1]["text"] == texts.UNAUTHORIZED
    with migrated_database.session() as session:
        assert session.get(Payment, seed["payment_id"]).evidence_state == "submitted"


def test_active_accountant_can_use_every_financial_command(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1006001
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    gateway.set_member(chat_id, 20, "member")
    with migrated_database.session() as session:
        reviewer, _ = UserRepository(session).get_or_create(20, "حسابدار", None)
        MembershipRepository(session, seed["group_id"]).set_accountant(
            user_id=reviewer.id,
            active=True,
            actor_user_id=seed["actor_id"],
        )

    for update_id, command in enumerate(
        ("/reports", "/summary", "/pending", "/verify", "/receipts", "/transactions", "/balance"),
        start=2,
    ):
        sent = run_command(
            handler,
            gateway,
            update_id=update_id,
            chat_id=chat_id,
            command=command,
            actor_id=20,
        )
        assert sent["text"] != texts.UNAUTHORIZED


def test_reject_workflow_records_reason_and_removes_countability(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1006101
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)

    pending = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/verify")
    payment_button = find_button(pending["reply_markup"], "رسید #")["callback_data"]
    detail = click(handler, gateway, update_id=3, chat_id=chat_id, data=payment_button)
    reject = find_button(detail["reply_markup"], "رد")["callback_data"]
    click(handler, gateway, update_id=4, chat_id=chat_id, data=reject)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=None,
        text="مرجع تراکنش با مدرک ارائه‌شده همخوان نیست",
        update_id=5,
    )
    click(handler, gateway, update_id=6, chat_id=chat_id, data=final)

    with migrated_database.session() as session:
        payment = session.get(Payment, seed["payment_id"])
        review = session.scalar(select(Review).where(Review.target_id == payment.id))
        assert payment.evidence_state == "rejected"
        assert review.decision == "rejected"
        assert review.comment == "مرجع تراکنش با مدرک ارائه‌شده همخوان نیست"


def test_receipt_pagination_and_financial_callback_actor_binding(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1006201
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    now = utc_now()
    with migrated_database.session() as session:
        ledger = LedgerService(session, seed["group_id"])
        for index in range(5):
            ledger.create_payment(
                money=Money(1_000_000 + index, CurrencyUnit.RIAL),
                counterparty_id=seed["party_id"],
                evidence_state=PaymentEvidenceState.SUBMITTED,
                actor_user_id=seed["actor_id"],
                recorded_at=now + timedelta(seconds=index + 1),
            )

    first = run_command(handler, gateway, update_id=2, chat_id=chat_id, command="/receipts")
    next_page = find_button(first["reply_markup"], "صفحه بعد")["callback_data"]
    second = click(handler, gateway, update_id=3, chat_id=chat_id, data=next_page)
    assert "صفحه قبل" in str(second["reply_markup"])
    assert second["text"].count("• رسید #") == 1

    gateway.set_member(chat_id, 20, "member")
    forged = click(
        handler,
        gateway,
        update_id=4,
        chat_id=chat_id,
        data=next_page,
        actor_id=20,
    )
    assert forged["text"] == texts.CALLBACK_EXPIRED


def test_transactions_show_active_associations_and_receipt_drilldown(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database,
        gateway,
        access_policy=TEST_ACCESS_POLICY,
        bot_username="receipt_test_bot",
    )
    chat_id = -1006301
    activate(handler, gateway, chat_id=chat_id)
    seed = seed_finance(migrated_database, chat_id=chat_id)
    with migrated_database.session() as session:
        LedgerService(session, seed["group_id"]).allocate(
            request_id=seed["request_id"],
            payment_id=seed["payment_id"],
            allocated_rial=60_000_000,
            actor_user_id=seed["actor_id"],
        )

    sent = run_command(
        handler, gateway, update_id=2, chat_id=chat_id, command="/transactions"
    )

    assert "رسیدهای #۱" in sent["text"]
    assert "درخواست‌های #۱" in sent["text"]
    assert find_button(sent["reply_markup"], "بررسی رسید #")
    assert find_button(sent["reply_markup"], "جزئیات خصوصی")


def test_reply_workflows_capture_request_and_promise_without_counting_promise_as_payment(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, access_policy=TEST_ACCESS_POLICY)
    chat_id = -1006401
    activate(handler, gateway, chat_id=chat_id)

    asyncio.run(
        handler.handle(
            group_message(
                2,
                2,
                chat_id=chat_id,
                actor=user(20, "الناز"),
                text="لطفاً ۶۰۰ هزار تومان برای من ثبت کنید",
            )
        )
    )
    asyncio.run(
        handler.handle(
            group_message(
                3,
                3,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text="/request",
                reply_to_message_id=2,
            )
        )
    )
    request_confirmation = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=None,
        text="۶۰۰٬۰۰۰ تومان | درخواست اعلام‌شده در پیام اصلی",
        update_id=4,
    )
    request_result = click(
        handler,
        gateway,
        update_id=5,
        chat_id=chat_id,
        data=request_confirmation,
    )
    assert "درخواست #" in request_result["text"]

    asyncio.run(
        handler.handle(
            group_message(
                6,
                6,
                chat_id=chat_id,
                actor=user(21, "امیررضا"),
                text="فردا ۱۰۰ هزار تومان پرداخت می‌کنم",
            )
        )
    )
    asyncio.run(
        handler.handle(
            group_message(
                7,
                7,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text="/promise",
                reply_to_message_id=6,
            )
        )
    )
    promise_confirmation = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=None,
        text="۱۰۰٬۰۰۰ تومان | وعده اعلام‌شده توسط فرستنده",
        update_id=8,
    )
    promise_result = click(
        handler,
        gateway,
        update_id=9,
        chat_id=chat_id,
        data=promise_confirmation,
    )
    assert "پرداخت محسوب نمی‌شود" in promise_result["text"]

    asyncio.run(
        handler.handle(
            group_message(
                10,
                10,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text="/promise",
                reply_to_message_id=2,
            )
        )
    )
    assert "قبلاً" in gateway.sent[-1]["text"]

    with migrated_database.session() as session:
        request = session.scalar(select(PaymentRequest))
        promise = session.scalar(select(PaymentPromise))
        request_party = session.get(Counterparty, request.counterparty_id)
        promise_party = session.get(Counterparty, promise.counterparty_id)
        assert request.amount_rial == 6_000_000
        assert promise.amount_rial == 1_000_000
        assert request.source_message_id is not None
        assert promise.source_message_id is not None
        assert request_party.display_name == "الناز"
        assert promise_party.display_name == "امیررضا"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        request_revision = session.scalar(
            select(RecordRevision).where(
                RecordRevision.target_type == "payment_request",
                RecordRevision.target_id == request.id,
            )
        )
        promise_revision = session.scalar(
            select(RecordRevision).where(
                RecordRevision.target_type == "payment_promise",
                RecordRevision.target_id == promise.id,
            )
        )
        assert request_revision.reason == "درخواست اعلام‌شده در پیام اصلی"
        assert promise_revision.reason == "وعده اعلام‌شده توسط فرستنده"


def test_help_lists_all_step_five_commands() -> None:
    for command in (
        "/reports",
        "/summary",
        "/pending",
        "/verify",
        "/receipts",
        "/transactions",
        "/balance",
        "/request",
        "/promise",
    ):
        assert command in texts.HELP_GROUP
