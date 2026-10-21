from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from ledger_helpers import LedgerSeed
from sqlalchemy import func, select
from telegram_fakes import FakeTelegramGateway

from receipt_bot.cleanup import CleanupConflict, ReceiptCleanupService
from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import Money
from receipt_bot.domain.time import utc_now
from receipt_bot.persistence.models import (
    AuditEvent,
    ExtractionAttempt,
    Payment,
    PaymentEvidence,
    PaymentRequest,
    ReceiptImage,
    ReceiptJob,
    RecordRevision,
    ReportRun,
    RequestPaymentAllocation,
    Review,
    TelegramGroup,
    TelegramUser,
)
from receipt_bot.persistence.repositories import (
    GroupRepository,
    MembershipRepository,
    MessageRepository,
    UserRepository,
)
from receipt_bot.reconciliation import LedgerService
from receipt_bot.reporting import ReportingEngine, render_persian_report
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram import texts
from receipt_bot.telegram.financial import FinancialWorkflowService
from receipt_bot.telegram.handler import TelegramUpdateHandler


def add_receipt(
    session,
    storage: ManagedStorage,
    *,
    group_id: int,
    source_message_id: int,
    key: str,
    created_at,
    data: bytes = b"receipt-image",
    write_file: bool = True,
    state: str = "present",
) -> ReceiptImage:
    if write_file:
        storage.write_bytes_atomic(key, data)
    item = ReceiptImage(
        group_id=group_id,
        source_message_id=source_message_id,
        telegram_file_id=f"file-{key}",
        telegram_file_unique_id=f"unique-{group_id}-{source_message_id}-{key}",
        original_filename="receipt.jpg",
        mime_type="image/jpeg",
        byte_size=len(data),
        sha256=(f"{group_id:02x}{source_message_id:02x}" * 32)[:64],
        storage_key=key,
        storage_state=state,
        created_at=created_at,
    )
    session.add(item)
    session.flush()
    return item


def seed_group(session, *, chat_id: int, now):
    group = GroupRepository(session).add(chat_id, f"گروه {chat_id}")
    user = UserRepository(session).add(abs(chat_id), "مدیر")
    message, _ = MessageRepository(session).add_idempotent(
        group_id=group.id,
        telegram_message_id=1,
        telegram_update_id=abs(chat_id) * 100,
        occurred_at=now,
        message_type="photo",
        sender_user_id=user.id,
    )
    return group, user, message


def find_button(markup: dict, label: str) -> dict:
    return next(
        button
        for row in markup["inline_keyboard"]
        for button in row
        if button["text"] == label
    )


def group_message(update_id: int, *, chat_id: int, actor_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(utc_now().timestamp()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "گروه پاک‌سازی"},
            "from": {"id": actor_id, "is_bot": False, "first_name": "کاربر"},
            "text": text,
        },
    }


def callback_update(update_id: int, *, chat_id: int, actor_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cleanup-{update_id}",
            "from": {"id": actor_id, "is_bot": False, "first_name": "کاربر"},
            "data": data,
            "message": {
                "message_id": update_id + 10_000,
                "date": int(utc_now().timestamp()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "گروه پاک‌سازی"},
            },
        },
    }


def test_preview_is_group_scoped_age_based_and_terminal_only(migrated_database, tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    now = utc_now()
    with migrated_database.session() as session:
        first, _, first_message = seed_group(session, chat_id=-7001, now=now)
        second, _, second_message = seed_group(session, chat_id=-7002, now=now)
        eligible = add_receipt(
            session,
            storage,
            group_id=first.id,
            source_message_id=first_message.id,
            key="group-1/eligible.jpg",
            created_at=now - timedelta(days=184),
            data=b"eligible",
        )
        add_receipt(
            session,
            storage,
            group_id=first.id,
            source_message_id=first_message.id,
            key="group-1/fresh.jpg",
            created_at=now - timedelta(days=182),
            data=b"fresh",
        )
        add_receipt(
            session,
            storage,
            group_id=second.id,
            source_message_id=second_message.id,
            key="group-2/old.jpg",
            created_at=now - timedelta(days=400),
            data=b"other-group",
        )
        add_receipt(
            session,
            storage,
            group_id=first.id,
            source_message_id=first_message.id,
            key="group-1/deleted.jpg",
            created_at=now - timedelta(days=400),
            data=b"metadata-only",
            write_file=False,
            state="deleted",
        )
        active_message, _ = MessageRepository(session).add_idempotent(
            group_id=first.id,
            telegram_message_id=2,
            telegram_update_id=700_102,
            occurred_at=now - timedelta(days=400),
            message_type="photo",
            sender_user_id=None,
        )
        active_receipt = add_receipt(
            session,
            storage,
            group_id=first.id,
            source_message_id=active_message.id,
            key="group-1/still-extracting.jpg",
            created_at=now - timedelta(days=400),
            data=b"still-needed",
        )
        session.add(
            ReceiptJob(
                group_id=first.id,
                source_message_id=active_message.id,
                receipt_id=active_receipt.id,
                telegram_file_id="active-file",
                telegram_file_unique_id="active-unique",
                status="extracting",
            )
        )

    preview = ReceiptCleanupService(migrated_database, storage).preview(first.id, now=now)

    assert preview.retention_days == 183
    assert [item.receipt_id for item in preview.candidates] == [eligible.id]
    assert preview.total_bytes == len(b"eligible")


def test_cleanup_deletes_only_file_and_preserves_accounting_records(
    migrated_database, tmp_path: Path
) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    now = utc_now()
    with migrated_database.session() as session:
        seed = LedgerSeed(session, chat_id=-7101, title="حسابداری", actor_telegram_id=7101)
        message = seed.message("رسید قدیمی", now - timedelta(days=200))
        receipt = add_receipt(
            session,
            storage,
            group_id=seed.group.id,
            source_message_id=message.id,
            key="group-1/old.jpg",
            created_at=now - timedelta(days=200),
            data=b"old-financial-image",
        )
        evidence = PaymentEvidence(
            group_id=seed.group.id,
            source_message_id=message.id,
            evidence_kind="image",
            receipt_image_id=receipt.id,
            content_sha256="a" * 64,
            classification_state="receipt_candidate",
        )
        session.add(evidence)
        session.flush()
        session.add(
            ExtractionAttempt(
                group_id=seed.group.id,
                evidence_id=evidence.id,
                receipt_id=receipt.id,
                attempt_number=1,
                provider="test",
                model="test",
                schema_version="receipt-v3",
                prompt_version="receipt-fa-v3",
                status="succeeded",
                normalized_candidate={
                    "amount_value": 1_000_000,
                    "amount_unit": "rial",
                    "amount_rial": 1_000_000,
                    "requires_review": False,
                },
                validation_issues=[],
                uncertainty={},
            )
        )
        session.flush()
        ledger = LedgerService(session, seed.group.id)
        party = ledger.add_counterparty("شخص نمونه")
        request = ledger.create_request(
            counterparty_id=party.id,
            requested_at=message.occurred_at,
            money=Money(1_000_000, CurrencyUnit.RIAL),
            source_message_id=message.id,
            actor_user_id=seed.actor.id,
        )
        payment = ledger.create_payment_from_evidence(evidence.id, actor_user_id=seed.actor.id)
        ledger.correct_payment(
            payment.id,
            reviewer_user_id=seed.actor.id,
            reason="تأیید مبلغ",
            counterparty_id=party.id,
        )
        ledger.allocate(
            request_id=request.id,
            payment_id=payment.id,
            allocated_rial=1_000_000,
            actor_user_id=seed.actor.id,
        )
        projection = ReportingEngine(session, seed.group.id).build(
            period_start=now - timedelta(days=201),
            period_end=now + timedelta(days=1),
        )
        ReportingEngine(session, seed.group.id).record_run(
            projection,
            requested_by_user_id=seed.actor.id,
            rendered_output=render_persian_report(projection),
        )
        group_id, actor_id, receipt_id = seed.group.id, seed.actor.id, receipt.id
        tables = (
            PaymentEvidence,
            ExtractionAttempt,
            Payment,
            PaymentRequest,
            RequestPaymentAllocation,
            Review,
            RecordRevision,
            ReportRun,
        )
        before = {
            table.__tablename__: session.scalar(select(func.count()).select_from(table))
            for table in tables
        }
        before_audits = [
            (item.id, item.action, item.target_type, item.target_id, item.metadata_json)
            for item in session.scalars(select(AuditEvent).order_by(AuditEvent.id))
        ]

    service = ReceiptCleanupService(migrated_database, storage)
    preview = service.preview(group_id, now=now)
    result = service.execute(preview, actor_user_id=actor_id, now=now)

    assert result.deleted_count == 1
    assert result.deleted_bytes == len(b"old-financial-image")
    assert not storage.resolve_key("group-1/old.jpg").exists()
    with migrated_database.session() as session:
        receipt = session.get(ReceiptImage, receipt_id)
        assert receipt.storage_state == "deleted"
        assert receipt.storage_key == "group-1/old.jpg"
        assert receipt.cleaned_at is not None
        after = {
            table.__tablename__: session.scalar(select(func.count()).select_from(table))
            for table in tables
        }
        assert after == before
        preserved_audits = [
            (item.id, item.action, item.target_type, item.target_id, item.metadata_json)
            for item in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action != "receipt_images.cleanup_executed")
                .order_by(AuditEvent.id)
            )
        ]
        assert preserved_audits == before_audits
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "receipt_images.cleanup_executed")
        )
        assert audit.metadata_json["deleted_count"] == 1
    detail = FinancialWorkflowService(migrated_database).payment_detail(
        group_id, payment.id
    )
    assert "تصویر اصلی طبق سیاست نگهداری حذف شده است" in detail


def test_missing_unsafe_directory_and_partial_failure_are_recoverable(
    migrated_database, tmp_path: Path
) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    storage.ensure_exists()
    now = utc_now()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do-not-delete")
    (storage.root / "directory-target").mkdir()
    with migrated_database.session() as session:
        group, actor, message = seed_group(session, chat_id=-7201, now=now)
        valid = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="group-1/valid.jpg",
            created_at=now - timedelta(days=300),
            data=b"valid",
        )
        missing = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="group-1/missing.jpg",
            created_at=now - timedelta(days=300),
            data=b"missing",
            write_file=False,
        )
        traversal = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="../outside.txt",
            created_at=now - timedelta(days=300),
            data=b"unsafe",
            write_file=False,
        )
        directory = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="directory-target",
            created_at=now - timedelta(days=300),
            data=b"directory",
            write_file=False,
        )
        group_id, actor_id = group.id, actor.id
        ids = [valid.id, missing.id, traversal.id, directory.id]

    service = ReceiptCleanupService(migrated_database, storage)
    result = service.execute(service.preview(group_id, now=now), actor_user_id=actor_id, now=now)

    assert result.deleted_count == 1
    assert result.missing_count == 1
    assert result.failed_count == 2
    assert outside.read_bytes() == b"do-not-delete"
    assert (storage.root / "directory-target").is_dir()
    with migrated_database.session() as session:
        states = {
            item.id: (item.storage_state, item.cleanup_error)
            for item in session.scalars(select(ReceiptImage).where(ReceiptImage.id.in_(ids)))
        }
    assert states[valid.id] == ("deleted", None)
    assert states[missing.id] == ("missing", "file_missing")
    assert states[traversal.id] == ("delete_failed", "unsafe_storage_path")
    assert states[directory.id] == ("delete_failed", "unsafe_storage_path")


def test_symlink_target_is_never_deleted(migrated_database, tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    storage.ensure_exists()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = storage.root / "linked.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    now = utc_now()
    with migrated_database.session() as session:
        group, actor, message = seed_group(session, chat_id=-7301, now=now)
        receipt = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="linked.jpg",
            created_at=now - timedelta(days=300),
            data=b"outside",
            write_file=False,
        )
        group_id, actor_id, receipt_id = group.id, actor.id, receipt.id

    service = ReceiptCleanupService(migrated_database, storage)
    result = service.execute(service.preview(group_id, now=now), actor_user_id=actor_id, now=now)

    assert result.failed_count == 1
    assert outside.read_bytes() == b"outside"
    assert link.is_symlink()
    with migrated_database.session() as session:
        assert session.get(ReceiptImage, receipt_id).storage_state == "delete_failed"


def test_changed_preview_requires_a_new_confirmation(migrated_database, tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    now = utc_now()
    with migrated_database.session() as session:
        group, _, message = seed_group(session, chat_id=-7401, now=now)
        first = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="group-1/first.jpg",
            created_at=now - timedelta(days=300),
        )
        group_id = group.id
    service = ReceiptCleanupService(migrated_database, storage)
    preview = service.preview(group_id, now=now)
    with migrated_database.session() as session:
        message = session.scalar(select(ReceiptImage).where(ReceiptImage.id == first.id))
        source_message_id = message.source_message_id
        add_receipt(
            session,
            storage,
            group_id=group_id,
            source_message_id=source_message_id,
            key="group-1/second.jpg",
            created_at=now - timedelta(days=300),
        )

    with pytest.raises(CleanupConflict, match="eligibility changed"):
        service.refresh(preview.to_state(), now=now)
    assert storage.resolve_key("group-1/first.jpg").exists()
    assert storage.resolve_key("group-1/second.jpg").exists()


def test_cleanup_command_is_admin_only_and_requires_two_confirmations(
    migrated_database, tmp_path: Path
) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, storage=storage)
    chat_id = -1007501
    admin_id = 10
    gateway.set_member(chat_id, admin_id, "administrator")
    asyncio.run(
        handler.handle(group_message(1, chat_id=chat_id, actor_id=admin_id, text="/start"))
    )
    now = utc_now()
    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup).where(TelegramGroup.telegram_chat_id == chat_id))
        admin = session.scalar(select(TelegramUser).where(TelegramUser.telegram_user_id == admin_id))
        message, _ = MessageRepository(session).add_idempotent(
            group_id=group.id,
            telegram_message_id=500,
            telegram_update_id=75_500,
            occurred_at=now - timedelta(days=300),
            message_type="photo",
            sender_user_id=admin.id,
        )
        receipt = add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="group-1/telegram.jpg",
            created_at=now - timedelta(days=300),
        )
        receipt_id = receipt.id

    gateway.set_member(chat_id, 20, "member")
    asyncio.run(
        handler.handle(group_message(2, chat_id=chat_id, actor_id=20, text="/cleanup"))
    )
    assert gateway.sent[-1]["text"] == texts.UNAUTHORIZED

    asyncio.run(
        handler.handle(group_message(3, chat_id=chat_id, actor_id=admin_id, text="/cleanup"))
    )
    preview_message = gateway.sent[-1]
    assert "تعداد تصاویر واجد شرایط: ۱" in preview_message["text"]
    prepare = find_button(preview_message["reply_markup"], "ادامه و بررسی نهایی")
    assert storage.resolve_key("group-1/telegram.jpg").exists()

    asyncio.run(
        handler.handle(
            callback_update(4, chat_id=chat_id, actor_id=admin_id, data=prepare["callback_data"])
        )
    )
    final_message = gateway.sent[-1]
    delete = find_button(final_message["reply_markup"], "حذف قطعی تصاویر")
    assert storage.resolve_key("group-1/telegram.jpg").exists()

    asyncio.run(
        handler.handle(
            callback_update(5, chat_id=chat_id, actor_id=admin_id, data=delete["callback_data"])
        )
    )
    assert "حذف‌شده: ۱ فایل" in gateway.sent[-1]["text"]
    assert not storage.resolve_key("group-1/telegram.jpg").exists()
    with migrated_database.session() as session:
        assert session.get(ReceiptImage, receipt_id).storage_state == "deleted"

    asyncio.run(
        handler.handle(
            callback_update(6, chat_id=chat_id, actor_id=admin_id, data=delete["callback_data"])
        )
    )
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED


def test_accountant_cannot_execute_cleanup_callback(migrated_database, tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "receipts")
    gateway = FakeTelegramGateway()
    handler = TelegramUpdateHandler(migrated_database, gateway, storage=storage)
    chat_id = -1007601
    gateway.set_member(chat_id, 10, "administrator")
    asyncio.run(handler.handle(group_message(1, chat_id=chat_id, actor_id=10, text="/start")))
    now = utc_now()
    with migrated_database.session() as session:
        group = session.scalar(select(TelegramGroup).where(TelegramGroup.telegram_chat_id == chat_id))
        admin = session.scalar(select(TelegramUser).where(TelegramUser.telegram_user_id == 10))
        accountant, _ = UserRepository(session).get_or_create(20, "حسابدار", None)
        MembershipRepository(session, group.id).set_accountant(
            user_id=accountant.id,
            active=True,
            actor_user_id=admin.id,
        )
        message, _ = MessageRepository(session).add_idempotent(
            group_id=group.id,
            telegram_message_id=50,
            telegram_update_id=76_050,
            occurred_at=now - timedelta(days=300),
            message_type="photo",
            sender_user_id=admin.id,
        )
        add_receipt(
            session,
            storage,
            group_id=group.id,
            source_message_id=message.id,
            key="group-1/admin-only.jpg",
            created_at=now - timedelta(days=300),
        )

    asyncio.run(handler.handle(group_message(2, chat_id=chat_id, actor_id=10, text="/cleanup")))
    prepare = find_button(gateway.sent[-1]["reply_markup"], "ادامه و بررسی نهایی")
    asyncio.run(
        handler.handle(
            callback_update(3, chat_id=chat_id, actor_id=10, data=prepare["callback_data"])
        )
    )
    delete = find_button(gateway.sent[-1]["reply_markup"], "حذف قطعی تصاویر")
    gateway.set_member(chat_id, 10, "member")
    asyncio.run(
        handler.handle(
            callback_update(4, chat_id=chat_id, actor_id=10, data=delete["callback_data"])
        )
    )
    assert gateway.sent[-1]["text"] == texts.UNAUTHORIZED
    assert storage.resolve_key("group-1/admin-only.jpg").exists()

    gateway.set_member(chat_id, 20, "member")
    asyncio.run(
        handler.handle(
            callback_update(5, chat_id=chat_id, actor_id=20, data=delete["callback_data"])
        )
    )
    assert gateway.sent[-1]["text"] == texts.CALLBACK_EXPIRED
    asyncio.run(handler.handle(group_message(6, chat_id=chat_id, actor_id=20, text="/cleanup")))
    assert gateway.sent[-1]["text"] == texts.UNAUTHORIZED
    assert storage.resolve_key("group-1/admin-only.jpg").exists()
