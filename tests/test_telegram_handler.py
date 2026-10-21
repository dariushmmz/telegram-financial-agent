from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from receipt_bot.domain.time import utc_now
from receipt_bot.persistence.models import (
    AuditEvent,
    GroupMembership,
    InteractionSession,
    ReceiptJob,
    TelegramGroup,
    TelegramMessage,
    TelegramUpdate,
)
from receipt_bot.telegram import texts
from receipt_bot.telegram.handler import TelegramUpdateHandler
from telegram_fakes import FakeTelegramGateway


def user(user_id: int, name: str = "کاربر") -> dict:
    return {"id": user_id, "is_bot": False, "first_name": name}


def group_message(
    update_id: int,
    message_id: int,
    *,
    chat_id: int,
    actor: dict,
    text: str | None = None,
    **fields,
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1_797_000_000,
        "chat": {"id": chat_id, "type": "supergroup", "title": f"گروه {chat_id}"},
        "from": actor,
        **fields,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def callback_update(
    update_id: int,
    *,
    chat_id: int,
    actor: dict,
    data: str,
    callback_id: str | None = None,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id or f"cb-{update_id}",
            "from": actor,
            "data": data,
            "message": {
                "message_id": 9000 + update_id,
                "date": 1_797_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": f"گروه {chat_id}"},
            },
        },
    }


def activate(handler, gateway, *, chat_id=-1001, actor_id=10, update_id=1):
    gateway.set_member(chat_id, actor_id, "administrator")
    update = group_message(
        update_id,
        update_id,
        chat_id=chat_id,
        actor=user(actor_id, "مدیر"),
        text="/start",
    )
    assert asyncio.run(handler.handle(update)) is True
    return update


def find_button(markup: dict, label: str) -> dict:
    return next(
        button
        for row in markup["inline_keyboard"]
        for button in row
        if button["text"] == label
    )


def test_group_activation_and_update_message_idempotency(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway)
    update = activate(handler, gateway)
    assert asyncio.run(handler.handle(update)) is False
    assert gateway.sent[0]["text"] == texts.START_ACTIVATED
    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup))
        assert group.active is True
        assert session.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
        assert session.scalar(select(func.count()).select_from(TelegramMessage)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_non_admin_cannot_activate_or_open_settings(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway)
    gateway.set_member(-1101, 11, "member")
    start = group_message(1, 1, chat_id=-1101, actor=user(11), text="/start")
    asyncio.run(handler.handle(start))
    with migrated_database.session() as session:
        assert session.scalar(select(TelegramGroup)).active is False
    assert gateway.sent[-1]["text"] == texts.START_ADMIN_REQUIRED

    settings = group_message(2, 2, chat_id=-1101, actor=user(11), text="/settings")
    asyncio.run(handler.handle(settings))
    assert gateway.sent[-1]["text"] == texts.GROUP_INACTIVE


def test_receipt_update_is_queued_once_and_groups_are_isolated(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway)
    activate(handler, gateway, chat_id=-1201, actor_id=1, update_id=1)
    activate(handler, gateway, chat_id=-1202, actor_id=2, update_id=2)
    photo = [
        {"file_id": "small", "file_unique_id": "small-u", "width": 90, "height": 90, "file_size": 10},
        {"file_id": "large", "file_unique_id": "shared-u", "width": 900, "height": 900, "file_size": 1000},
    ]
    first = group_message(
        3,
        30,
        chat_id=-1201,
        actor=user(5),
        photo=photo,
        message_thread_id=77,
        reply_to_message={"message_id": 12},
    )
    second = group_message(4, 40, chat_id=-1202, actor=user(5), photo=photo)
    asyncio.run(handler.handle(first))
    asyncio.run(handler.handle(first))
    asyncio.run(handler.handle(second))
    with migrated_database.session() as session:
        jobs = list(session.scalars(select(ReceiptJob).order_by(ReceiptJob.id)))
        assert len(jobs) == 2
        assert jobs[0].group_id != jobs[1].group_id
        assert {job.telegram_file_id for job in jobs} == {"large"}
        assert session.scalar(select(func.count()).select_from(TelegramMessage)) == 4
        stored = session.scalar(
            select(TelegramMessage).where(TelegramMessage.telegram_message_id == 30)
        )
        assert stored.telegram_thread_id == 77
        assert stored.reply_to_message_id == 12


def test_settings_callback_rechecks_admin_and_rejects_forgery(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, bot_username="receipt_test_bot")
    activate(handler, gateway, chat_id=-1301, actor_id=10)
    settings_update = group_message(2, 2, chat_id=-1301, actor=user(10, "مدیر"), text="/settings")
    asyncio.run(handler.handle(settings_update))
    markup = gateway.sent[-1]["reply_markup"]
    retention_data = find_button(markup, "۹۰ روز")["callback_data"]

    gateway.set_member(-1301, 20, "member")
    asyncio.run(
        handler.handle(
            callback_update(3, chat_id=-1301, actor=user(20), data=retention_data)
        )
    )
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED

    gateway.set_member(-1301, 10, "member")
    asyncio.run(
        handler.handle(
            callback_update(4, chat_id=-1301, actor=user(10), data=retention_data)
        )
    )
    assert gateway.sent[-1]["text"] == texts.UNAUTHORIZED
    with migrated_database.session() as session:
        assert session.scalar(select(TelegramGroup)).retention_days == 183

    gateway.set_member(-1301, 10, "administrator")
    asyncio.run(
        handler.handle(
            callback_update(5, chat_id=-1301, actor=user(10), data=retention_data)
        )
    )
    assert gateway.sent[-1]["text"] == texts.retention_changed(90)
    with migrated_database.session() as session:
        assert session.scalar(select(TelegramGroup)).retention_days == 90
        actions = [event.action for event in session.scalars(select(AuditEvent).order_by(AuditEvent.id))]
        assert actions == ["group.activated", "group.retention_changed"]


def test_callback_cancel_back_and_expiry_paths(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway)
    activate(handler, gateway, chat_id=-1401, actor_id=10)

    asyncio.run(handler.handle(group_message(2, 2, chat_id=-1401, actor=user(10), text="/settings")))
    cancel = find_button(gateway.sent[-1]["reply_markup"], "لغو")["callback_data"]
    asyncio.run(handler.handle(callback_update(3, chat_id=-1401, actor=user(10), data=cancel)))
    assert gateway.sent[-1]["text"] == texts.CALLBACK_CANCELLED
    asyncio.run(handler.handle(callback_update(4, chat_id=-1401, actor=user(10), data=cancel)))
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED

    asyncio.run(handler.handle(group_message(5, 5, chat_id=-1401, actor=user(10), text="/settings")))
    back = find_button(gateway.sent[-1]["reply_markup"], "بازگشت")["callback_data"]
    prior_count = len(gateway.sent)
    asyncio.run(handler.handle(callback_update(6, chat_id=-1401, actor=user(10), data=back)))
    assert len(gateway.sent) == prior_count + 1
    assert gateway.sent[-1]["text"].startswith("⚙️ تنظیمات گروه")

    expiring = find_button(gateway.sent[-1]["reply_markup"], "لغو")["callback_data"]
    token = expiring.split(":", 2)[1]
    with migrated_database.session() as session:
        session.get(InteractionSession, token).expires_at = utc_now() - timedelta(seconds=1)
    asyncio.run(handler.handle(callback_update(7, chat_id=-1401, actor=user(10), data=expiring)))
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED
    assert len(gateway.answered_callbacks) == 4


def test_reviewer_selection_and_private_handoff_are_bound_to_actor(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, bot_username="receipt_test_bot")
    activate(handler, gateway, chat_id=-1501, actor_id=10)
    gateway.set_member(-1501, 30, "member", profile=user(30, "حسابدار"))
    asyncio.run(handler.handle(group_message(2, 2, chat_id=-1501, actor=user(10), text="/settings")))
    markup = gateway.sent[-1]["reply_markup"]
    add_data = find_button(markup, "افزودن حسابدار")["callback_data"]
    private_url = find_button(markup, "ادامه در گفت‌وگوی خصوصی")["url"]

    asyncio.run(handler.handle(callback_update(3, chat_id=-1501, actor=user(10), data=add_data)))
    request_id = gateway.sent[-1]["reply_markup"]["keyboard"][0][0]["request_users"]["request_id"]
    shared = group_message(
        4,
        4,
        chat_id=-1501,
        actor=user(10),
        users_shared={
            "request_id": request_id,
            "users": [{"user_id": 30, "first_name": "حسابدار"}],
        },
    )
    asyncio.run(handler.handle(shared))
    assert "حسابدار اضافه شد" in gateway.sent[-1]["text"]
    with migrated_database.session() as session:
        membership = session.scalar(
            select(GroupMembership).where(GroupMembership.user_id != 1, GroupMembership.role == "accountant")
        )
        assert membership.active is True

    token = parse_qs(urlparse(private_url).query)["start"][0]
    private_wrong = {
        "update_id": 5,
        "message": {
            "message_id": 5,
            "date": 1_797_000_000,
            "chat": {"id": 99, "type": "private"},
            "from": user(99),
            "text": f"/start {token}",
        },
    }
    asyncio.run(handler.handle(private_wrong))
    assert gateway.sent[-1]["text"] == texts.PRIVATE_HANDOFF_INVALID

    private_admin = {
        "update_id": 6,
        "message": {
            "message_id": 6,
            "date": 1_797_000_000,
            "chat": {"id": 10, "type": "private"},
            "from": user(10),
            "text": f"/start {token}",
        },
    }
    asyncio.run(handler.handle(private_admin))
    assert gateway.sent[-1]["text"] == texts.PRIVATE_HANDOFF_OK


def test_persian_basic_command_contract() -> None:
    assert texts.START_ACTIVATED.startswith("✅ ربات ثبت رسید")
    assert "/settings" in texts.START_ACTIVATED
    assert "تأیید وصول بانکی نیست" in texts.START_ACTIVATED
    assert texts.HELP_GROUP.splitlines()[0] == "راهنمای ربات مالی"
    assert "/reports" in texts.HELP_GROUP
    assert texts.UNAUTHORIZED == "⛔ شما مجوز انجام این عملیات را ندارید."
