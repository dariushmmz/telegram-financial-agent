from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from telegram_fakes import FakeTelegramGateway
from test_telegram_handler import callback_update, find_button, group_message, user

from receipt_bot.domain.enums import ReceiptJobState
from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.models import (
    ExtractionAttempt,
    ReceiptJob,
    TelegramGroup,
    TelegramUpdate,
    TelegramUser,
    TextReceiptJob,
)
from receipt_bot.persistence.repositories import TelegramUpdateRepository
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram import texts
from receipt_bot.telegram.access import (
    GROUP_AUTHORIZER_USER_ID_SETTING,
    GROUP_AUTHORIZER_USERNAME_SETTING,
    TelegramAccessPolicy,
)
from receipt_bot.telegram.handler import TelegramUpdateHandler
from receipt_bot.telegram.worker import ReceiptJobWorker

RECEIPT_TEXT = """تراکنش موفق
انتقال وجه آزمایشی
مبلغ: ۱٬۰۰۰ ریال
شماره مرجع: ۱۲۳۴۵۶"""


class NeverCalledProvider:
    name = "never-called"
    model = "never-called-v1"

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, _image: PreparedImage):
        self.calls += 1
        raise AssertionError("image provider must not be called for an unauthorized group")

    def extract_text(self, _text: str):
        self.calls += 1
        raise AssertionError("text provider must not be called for an unauthorized group")


def policy(*user_ids: int) -> TelegramAccessPolicy:
    return TelegramAccessPolicy(frozenset(f"user_{user_id}" for user_id in user_ids))


def handler_for(database, gateway, *user_ids: int) -> TelegramUpdateHandler:
    return TelegramUpdateHandler(database, gateway, access_policy=policy(*user_ids))


def assert_support_button(message: dict) -> None:
    button = find_button(message["reply_markup"], texts.SUPPORT_BUTTON_LABEL)
    assert button == {"text": "پشتیبانی ربات", "url": "https://t.me/amixrmz"}
    assert texts.SUPPORT_USERNAME in message["text"]


def test_username_allowlist_accepts_at_prefix_and_is_case_insensitive() -> None:
    access = TelegramAccessPolicy(frozenset({"amixrmz"}))

    assert access.allows_user("@AMIXRMZ")
    assert access.allows_user("amixrmz")
    assert not access.allows_user("different_user")
    assert not access.allows_user(None)


def test_only_allowlisted_current_admin_can_register_and_authorize_group(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = handler_for(migrated_database, gateway, 101)

    gateway.set_member(-4101, 999, "administrator")
    rejected = group_message(1, 1, chat_id=-4101, actor=user(999), text="/start")
    assert asyncio.run(handler.handle(rejected)) is False
    assert gateway.member_checks == []
    assert gateway.sent[-1]["text"] == texts.ACCESS_RESTRICTED
    assert_support_button(gateway.sent[-1])

    missing_username_actor = user(101)
    missing_username_actor.pop("username")
    gateway.set_member(-4104, 101, "administrator")
    missing_username = group_message(
        4,
        4,
        chat_id=-4104,
        actor=missing_username_actor,
        text="/start",
    )
    assert asyncio.run(handler.handle(missing_username)) is False
    assert gateway.member_checks == []
    assert gateway.sent[-1]["text"] == texts.ACCESS_RESTRICTED

    gateway.set_member(-4102, 101, "member")
    non_admin = group_message(2, 2, chat_id=-4102, actor=user(101), text="/start")
    assert asyncio.run(handler.handle(non_admin)) is False
    assert gateway.sent[-1]["text"] == texts.START_ADMIN_REQUIRED
    assert_support_button(gateway.sent[-1])

    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(TelegramGroup)) == 0
        assert session.scalar(select(func.count()).select_from(TelegramUser)) == 0
        assert session.scalar(select(func.count()).select_from(TelegramUpdate)) == 0

    gateway.set_member(-4103, 101, "administrator")
    accepted = group_message(
        3,
        3,
        chat_id=-4103,
        actor=user(101, username="USER_101"),
        text="/start",
    )
    assert asyncio.run(handler.handle(accepted)) is True
    assert gateway.sent[-1]["text"] == texts.START_ACTIVATED
    assert_support_button(gateway.sent[-1])

    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup))
        assert group.active is True
        assert group.settings[GROUP_AUTHORIZER_USERNAME_SETTING] == "user_101"
        assert group.settings[GROUP_AUTHORIZER_USER_ID_SETTING] == 101
        assert group.settings_version == 2


def test_authorized_group_members_can_submit_and_support_is_in_main_help_and_settings(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    handler = handler_for(migrated_database, gateway, 101)
    chat_id = -4201
    gateway.set_member(chat_id, 101, "administrator")

    asyncio.run(handler.handle(group_message(1, 1, chat_id=chat_id, actor=user(101), text="/start")))
    assert_support_button(gateway.sent[-1])

    asyncio.run(handler.handle(group_message(2, 2, chat_id=chat_id, actor=user(202), text=RECEIPT_TEXT)))
    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(TextReceiptJob)) == 1

    asyncio.run(handler.handle(group_message(3, 3, chat_id=chat_id, actor=user(202), text="/help")))
    assert gateway.sent[-1]["text"] == texts.HELP_GROUP
    assert_support_button(gateway.sent[-1])

    asyncio.run(handler.handle(group_message(4, 4, chat_id=chat_id, actor=user(101), text="/settings")))
    assert gateway.sent[-1]["text"].startswith("⚙️ تنظیمات گروه")
    assert_support_button(gateway.sent[-1])


def test_unauthorized_group_updates_and_recovered_callbacks_are_discarded(
    migrated_database,
) -> None:
    gateway = FakeTelegramGateway()
    original = handler_for(migrated_database, gateway, 101)
    chat_id = -4301
    gateway.set_member(chat_id, 101, "administrator")
    asyncio.run(original.handle(group_message(1, 1, chat_id=chat_id, actor=user(101), text="/start")))
    asyncio.run(original.handle(group_message(2, 2, chat_id=chat_id, actor=user(101), text="/settings")))
    retention = find_button(gateway.sent[-1]["reply_markup"], "۹۰ روز")["callback_data"]

    recovered_callback = callback_update(3, chat_id=chat_id, actor=user(101), data=retention)
    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup))
        TelegramUpdateRepository(session).register(
            update_id=3,
            update_kind="callback_query",
            payload=recovered_callback,
            group_id=group.id,
        )

    revoked = handler_for(migrated_database, gateway, 999)
    assert asyncio.run(revoked.handle(recovered_callback)) is False
    assert gateway.answered_callbacks[-1]["id"] == "cb-3"
    assert gateway.sent[-1]["text"] == texts.ACCESS_RESTRICTED
    assert_support_button(gateway.sent[-1])

    ignored_receipt = group_message(4, 4, chat_id=-4302, actor=user(404), text=RECEIPT_TEXT)
    assert asyncio.run(revoked.handle(ignored_receipt)) is False

    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup).where(TelegramGroup.telegram_chat_id == chat_id))
        recovered = session.get(TelegramUpdate, 3)
        assert group.retention_days == 183
        assert recovered.status == "completed"
        assert recovered.payload == {}
        assert session.scalar(select(func.count()).select_from(TextReceiptJob)) == 0
        assert session.scalar(select(func.count()).select_from(TelegramGroup)) == 1


def test_revoked_group_recovered_jobs_never_download_or_call_ai(
    migrated_database, tmp_path
) -> None:
    gateway = FakeTelegramGateway()
    authorized = handler_for(migrated_database, gateway, 101)
    chat_id = -4401
    gateway.set_member(chat_id, 101, "administrator")
    asyncio.run(authorized.handle(group_message(1, 1, chat_id=chat_id, actor=user(101), text="/start")))

    asyncio.run(
        authorized.handle(
            group_message(
                2,
                2,
                chat_id=chat_id,
                actor=user(202),
                document={
                    "file_id": "must-not-download",
                    "file_unique_id": "blocked-file",
                    "file_size": 100,
                    "mime_type": "image/png",
                    "file_name": "synthetic.png",
                },
            )
        )
    )
    asyncio.run(authorized.handle(group_message(3, 3, chat_id=chat_id, actor=user(202), text=RECEIPT_TEXT)))

    with migrated_database.session() as session:
        image_job = session.scalar(select(ReceiptJob))
        image_job.status = ReceiptJobState.DOWNLOADING.value
        image_job.processing_started_at = utc_now() - timedelta(minutes=30)

    provider = NeverCalledProvider()
    worker = ReceiptJobWorker(
        migrated_database,
        gateway,
        ManagedStorage(tmp_path / "receipts"),
        ExtractionService(provider),
        access_policy=policy(999),
        retry_delay_seconds=0,
    )

    assert worker.recover_stale(older_than_minutes=10) == 1
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False

    assert provider.calls == 0
    assert gateway.download_requests == []
    with migrated_database.session() as session:
        image_job = session.scalar(select(ReceiptJob))
        text_job = session.scalar(select(TextReceiptJob))
        assert image_job.status == ReceiptJobState.FAILED_FINAL.value
        assert text_job.status == ReceiptJobState.FAILED_FINAL.value
        assert image_job.last_error_code == "group_unauthorized"
        assert text_job.last_error_code == "group_unauthorized"
        assert session.scalar(select(func.count()).select_from(ExtractionAttempt)) == 0


def test_private_support_is_available_without_registration(migrated_database) -> None:
    gateway = FakeTelegramGateway()
    handler = handler_for(migrated_database, gateway, 101)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1_797_000_000,
            "chat": {"id": 999, "type": "private"},
            "from": user(999),
            "text": "/start",
        },
    }

    assert asyncio.run(handler.handle(update)) is True
    assert gateway.sent[-1]["text"] == texts.START_PRIVATE
    assert_support_button(gateway.sent[-1])
    with migrated_database.session() as session:
        assert session.scalar(select(func.count()).select_from(TelegramUser)) == 0
        assert session.scalar(select(func.count()).select_from(TelegramUpdate)) == 0
