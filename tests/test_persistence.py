from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from receipt_bot.persistence.models import AuditEvent, RecordRevision, TelegramMessage
from receipt_bot.persistence.repositories import (
    FinanceRepository,
    GroupRepository,
    HistoryRepository,
    MessageRepository,
)


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def test_message_ingestion_is_idempotent(migrated_database) -> None:
    with migrated_database.session() as session:
        group = GroupRepository(session).add(-1001, "گروه آزمایشی")
        repository = MessageRepository(session)
        first, first_created = repository.add_idempotent(
            group_id=group.id,
            telegram_message_id=55,
            telegram_update_id=999,
            occurred_at=NOW,
            message_type="text",
            text="درخواست پرداخت",
        )
        second, second_created = repository.add_idempotent(
            group_id=group.id,
            telegram_message_id=55,
            telegram_update_id=999,
            occurred_at=NOW,
            message_type="text",
            text="متن تکراری",
        )
        assert first.id == second.id
        assert first_created is True
        assert second_created is False
        assert session.scalar(select(func.count()).select_from(TelegramMessage)) == 1


def test_database_unique_constraint_is_second_idempotency_barrier(migrated_database) -> None:
    with pytest.raises(IntegrityError):
        with migrated_database.session() as session:
            group = GroupRepository(session).add(-1002, "گروه")
            session.add_all(
                [
                    TelegramMessage(
                        group_id=group.id,
                        telegram_message_id=1,
                        occurred_at=NOW,
                        message_type="text",
                    ),
                    TelegramMessage(
                        group_id=group.id,
                        telegram_message_id=1,
                        occurred_at=NOW,
                        message_type="text",
                    ),
                ]
            )


def test_cross_group_allocation_is_rejected_by_composite_foreign_keys(migrated_database) -> None:
    with migrated_database.session() as session:
        group_one = GroupRepository(session).add(-2001, "گروه یک")
        group_two = GroupRepository(session).add(-2002, "گروه دو")
        repo_one = FinanceRepository(session, group_one.id)
        repo_two = FinanceRepository(session, group_two.id)
        counterparty = repo_one.add_counterparty("الناز")
        request = repo_one.add_request(
            counterparty_id=counterparty.id,
            requested_at=NOW,
            amount_value=600_000_000,
            amount_unit="toman",
            amount_rial=6_000_000_000,
        )
        payment_other_group = repo_two.add_payment(
            amount_value=600_000_000,
            amount_unit="toman",
            amount_rial=6_000_000_000,
        )
        request_id = request.id
        other_payment_id = payment_other_group.id

    with pytest.raises(IntegrityError):
        with migrated_database.session() as session:
            FinanceRepository(session, group_one.id).allocate(
                request_id=request_id,
                payment_id=other_payment_id,
                allocated_rial=6_000_000_000,
            )


def test_history_repository_and_database_are_append_only(migrated_database) -> None:
    with migrated_database.session() as session:
        group = GroupRepository(session).add(-3001, "حسابرسی")
        history = HistoryRepository(session, group.id)
        revision = history.append_revision(
            target_type="payment",
            target_id=10,
            revision_number=1,
            snapshot={"amount_rial": 1_000},
            reason="ثبت اولیه",
        )
        event = history.append_audit(
            action="payment.created",
            target_type="payment",
            target_id=10,
            metadata={"revision": 1},
        )
        revision_id = revision.id
        event_id = event.id
        assert [item.id for item in history.revisions_for("payment", 10)] == [revision_id]
        assert [item.id for item in history.audit_events()] == [event_id]
        assert not hasattr(history, "update")
        assert not hasattr(history, "delete")

    with pytest.raises(DatabaseError, match="append-only"):
        with migrated_database.session() as session:
            session.execute(
                text("UPDATE record_revisions SET reason='tampered' WHERE id=:id"),
                {"id": revision_id},
            )

    with pytest.raises(DatabaseError, match="append-only"):
        with migrated_database.session() as session:
            session.execute(text("DELETE FROM audit_events WHERE id=:id"), {"id": event_id})

    with migrated_database.session() as session:
        assert session.get(RecordRevision, revision_id).reason == "ثبت اولیه"
        assert session.get(AuditEvent, event_id).action == "payment.created"
