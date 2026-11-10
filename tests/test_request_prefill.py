from __future__ import annotations

import asyncio

from sqlalchemy import select

from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import normalize_digits
from receipt_bot.domain.request_parser import parse_request_announcement
from receipt_bot.persistence.models import Counterparty, PaymentRequest, TelegramMessage, TelegramUser
from receipt_bot.telegram.financial import FinancialWorkflowService, Period, parse_request_review_input
from receipt_bot.telegram.handler import TelegramUpdateHandler
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway
from test_telegram_financial_workflows import (
    activate,
    click,
    complete_prompt,
    find_button,
    group_message,
    run_command,
    user,
)


IBAN = "IR650560611828005998641501"
CARD = "6104337795012077"


def test_persian_announcement_parser_extracts_labeled_fields() -> None:
    candidate = parse_request_announcement(
        """لطفاً مبلغ ۶۶۷٬۳۰۰ ریال واریز شود
سپرده: IR - ۶۵ ۰۵۶۰ ۶۱۱۸ ۲۸۰۰ ۵۹۹۸ ۶۴۱۵ ۰۱
کارت: 6104-3377-9501-2077
به نام فاطمه محمدی
بانک سامان"""
    )
    assert candidate.amount_value == 667_300
    assert candidate.amount_unit is CurrencyUnit.RIAL
    assert candidate.account_holder == "فاطمه محمدی"
    assert candidate.destination_iban == IBAN
    assert candidate.destination_card == CARD
    assert candidate.destination_bank == "سامان"
    assert candidate.complete_for_request


def test_parser_never_chooses_ambiguous_or_malformed_values() -> None:
    candidate = parse_request_announcement(
        """مبلغ ۱۰۰٬۰۰۰ تومان و مبلغ ۲۰۰٬۰۰۰ ریال
به نام علی رضایی
بنام رضا محمدی
کارت: 1111-1111-1111-1111
شبا: IR000000000000000000000000"""
    )
    assert candidate.amount_value is None
    assert candidate.amount_unit is CurrencyUnit.UNKNOWN
    assert candidate.account_holder is None
    assert candidate.destination_card is None
    assert candidate.destination_iban is None
    assert {"amount", "account_holder"}.issubset(candidate.ambiguous_fields)
    assert not candidate.complete_for_request

    missing_unit = parse_request_announcement(
        f"مبلغ ۵۰۰۰۰۰\nبه نام مریم احمدی\nشبا: {IBAN}"
    )
    assert missing_unit.amount_value is None
    assert missing_unit.account_holder == "مریم احمدی"
    assert not missing_unit.complete_for_request


def test_manual_review_parser_can_correct_every_material_field() -> None:
    reviewed = parse_request_review_input(
        f"۷۰۰٬۰۰۰ تومان | مریم احمدی | شبا:{IBAN}؛ کارت:{CARD}؛ بانک:سامان | بررسی اصل پیام"
    )
    assert reviewed["amount_value"] == 700_000
    assert reviewed["amount_unit"] == "toman"
    assert reviewed["account_holder"] == "مریم احمدی"
    assert reviewed["destination_iban"] == IBAN
    assert reviewed["destination_card"] == CARD
    assert reviewed["reason"] == "بررسی اصل پیام"


def test_request_reply_prefills_holder_separate_from_sender_and_reports_it(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    chat_id = -1007201
    activate(handler, gateway, chat_id=chat_id)

    announcement = (
        f"لطفاً مبلغ ۶۶۷٬۳۰۰ ریال واریز شود\n"
        f"شبا: {IBAN}\n"
        "به نام فاطمه محمدی\n"
        "بانک سامان"
    )
    asyncio.run(
        handler.handle(
            group_message(
                2,
                100,
                chat_id=chat_id,
                actor=user(22, "اعلام‌کننده"),
                text=announcement,
            )
        )
    )
    asyncio.run(
        handler.handle(
            group_message(
                3,
                101,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text="/request",
                reply_to_message_id=100,
            )
        )
    )
    preview = gateway.sent[-1]
    assert "فاطمه محمدی" in preview["text"]
    assert IBAN not in preview["text"]
    assert IBAN[-4:] in normalize_digits(preview["text"])
    assert "اعلام‌کننده" in preview["text"]
    confirm = find_button(preview["reply_markup"], "تأیید اطلاعات استخراج‌شده")[
        "callback_data"
    ]
    prompt = click(handler, gateway, update_id=4, chat_id=chat_id, data=confirm)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text="صاحب حساب و مبلغ با پیام اصلی تطبیق داده شد",
        update_id=5,
    )
    result = click(handler, gateway, update_id=6, chat_id=chat_id, data=final)
    assert "فاطمه محمدی" in result["text"]

    with migrated_database.session() as session:
        request = session.scalar(select(PaymentRequest))
        party = session.get(Counterparty, request.counterparty_id)
        source = session.get(TelegramMessage, request.source_message_id)
        sender = session.get(TelegramUser, source.sender_user_id)
        assert party.display_name == request.account_holder_name == "فاطمه محمدی"
        assert party.telegram_user_id is None
        assert sender.telegram_user_id == 22
        assert request.destination_iban == IBAN
        assert request.source_parser_candidate["account_holder"] == "فاطمه محمدی"
        group_id = request.group_id

    report = run_command(
        handler, gateway, update_id=7, chat_id=chat_id, command="/reports"
    )
    transactions = run_command(
        handler, gateway, update_id=8, chat_id=chat_id, command="/transactions"
    )
    assert "فاطمه محمدی" in report["text"]
    assert "فاطمه محمدی" in transactions["text"]
    assert "اعلام‌کننده" not in report["text"]

    private = FinancialWorkflowService(migrated_database).private_report_sources(
        group_id, Period.today()
    )
    assert IBAN in private


def test_incomplete_prefill_requires_correction_and_manual_request_still_works(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(
        migrated_database, gateway, access_policy=TEST_ACCESS_POLICY
    )
    chat_id = -1007202
    activate(handler, gateway, chat_id=chat_id)
    incomplete = f"مبلغ ۵۰۰۰۰۰\nبه نام مریم احمدی\nشبا: {IBAN}"
    asyncio.run(
        handler.handle(
            group_message(
                2,
                200,
                chat_id=chat_id,
                actor=user(23, "فرستنده متفاوت"),
                text=incomplete,
            )
        )
    )
    asyncio.run(
        handler.handle(
            group_message(
                3,
                201,
                chat_id=chat_id,
                actor=user(10, "مدیر"),
                text="/request",
                reply_to_message_id=200,
            )
        )
    )
    preview = gateway.sent[-1]
    assert not any(
        button["text"].startswith("تأیید اطلاعات")
        for row in preview["reply_markup"]["inline_keyboard"]
        for button in row
    )
    correct = find_button(preview["reply_markup"], "اصلاح/تکمیل")["callback_data"]
    click(handler, gateway, update_id=4, chat_id=chat_id, data=correct)
    final = complete_prompt(
        handler,
        gateway,
        chat_id=chat_id,
        prompt_message_id=len(gateway.sent),
        text=f"۵۰۰٬۰۰۰ تومان | مریم احمدی | شبا:{IBAN} | تکمیل واحد مبلغ",
        update_id=5,
    )
    click(handler, gateway, update_id=6, chat_id=chat_id, data=final)
    with migrated_database.session() as session:
        request = session.scalar(select(PaymentRequest))
        assert request.amount_rial == 5_000_000
        assert request.account_holder_name == "مریم احمدی"
