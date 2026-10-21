from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, sqlite_url
from sqlalchemy import func, select

from receipt_bot.config import AppSettings
from receipt_bot.domain.enums import CurrencyUnit
from receipt_bot.domain.money import Money
from receipt_bot.operations import (
    OperationError,
    backup_database,
    healthcheck,
    prepare_runtime,
    restore_database,
)
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import PaymentRequest, ReportRun
from receipt_bot.persistence.repositories import (
    GroupRepository,
    MessageRepository,
    UserRepository,
)
from receipt_bot.reconciliation import LedgerService
from receipt_bot.reporting import ReportingEngine


def settings_for(tmp_path: Path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        database_url=sqlite_url(tmp_path / "live" / "receipt_bot.db"),
        data_root=tmp_path / "managed",
        allowed_telegram_usernames="@operator_test",
        telegram_bot_token="test-token",
    )


def seed_report_fixture(database: Database) -> tuple[int, datetime, datetime, datetime, dict]:
    start = datetime(2026, 10, 20, tzinfo=UTC)
    end = start + timedelta(days=1)
    as_of = end + timedelta(hours=1)
    with database.session() as session:
        group = GroupRepository(session).add(-1007001, "گروه پشتیبان")
        actor = UserRepository(session).add(7001, "حسابدار")
        source, _ = MessageRepository(session).add_idempotent(
            group_id=group.id,
            telegram_message_id=1,
            telegram_update_id=1,
            occurred_at=start + timedelta(hours=2),
            message_type="text",
            sender_user_id=actor.id,
            text="درخواست آزمایشی",
        )
        ledger = LedgerService(session, group.id)
        counterparty = ledger.add_counterparty("مشتری آزمایشی", telegram_user_id=actor.id)
        ledger.create_request(
            counterparty_id=counterparty.id,
            requested_at=source.occurred_at,
            money=Money(25_000, CurrencyUnit.TOMAN),
            source_message_id=source.id,
            actor_user_id=actor.id,
        )
        engine = ReportingEngine(session, group.id)
        projection = engine.build(period_start=start, period_end=end, as_of=as_of)
        engine.record_run(
            projection,
            requested_by_user_id=actor.id,
            rendered_output="گزارش ثابت آزمایشی",
        )
        expected = asdict(projection)
    return group.id, start, end, as_of, expected


def test_prepare_runtime_migrates_fresh_database_and_healthchecks(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    report = prepare_runtime(settings, root=PROJECT_ROOT)

    assert report.status == "ok"
    assert report.revision == report.migration_head == "b6d91f4a2c38"
    assert report.integrity == "ok"
    assert report.foreign_key_violations == 0
    assert Path(report.database_path).is_file()
    assert Path(report.receipt_storage_path).is_dir()
    assert healthcheck(settings, root=PROJECT_ROOT) == report


def test_online_backup_and_separate_restore_preserve_report_fixture(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    prepare_runtime(settings, root=PROJECT_ROOT)
    live = Database(settings.database_url)
    group_id, start, end, as_of, expected = seed_report_fixture(live)

    backup_path = tmp_path / "backups" / "receipt_bot.sqlite"
    backup = backup_database(settings.database_url, backup_path)
    assert backup.integrity == "ok"
    assert backup.revision == "b6d91f4a2c38"
    assert backup.byte_size > 0
    assert len(backup.sha256) == 64

    restore_path = tmp_path / "restore" / "receipt_bot.db"
    restored = restore_database(backup_path, restore_path)
    assert restored.integrity == "ok"
    assert restored.sha256 == backup.sha256

    restored_database = Database(sqlite_url(restore_path))
    try:
        with restored_database.session() as session:
            actual = ReportingEngine(session, group_id).build(
                period_start=start,
                period_end=end,
                as_of=as_of,
            )
            assert asdict(actual) == expected
            assert session.scalar(select(func.count()).select_from(PaymentRequest)) == 1
            assert session.scalar(select(func.count()).select_from(ReportRun)) == 1
    finally:
        restored_database.dispose()
        live.dispose()


def test_backup_and_restore_refuse_overwrite_or_live_database_target(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    prepare_runtime(settings, root=PROJECT_ROOT)
    destination = tmp_path / "backup.db"
    backup_database(settings.database_url, destination)

    with pytest.raises(OperationError, match="already exists"):
        backup_database(settings.database_url, destination)
    with pytest.raises(OperationError, match="empty location"):
        restore_database(destination, Path(settings.database_url.removeprefix("sqlite:///")))


def test_healthcheck_rejects_unmigrated_database(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    database_path = Path(settings.database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True)
    database_path.touch()
    settings.receipt_storage_root.mkdir(parents=True)

    with pytest.raises(OperationError, match="diagnostics failed"):
        healthcheck(settings, root=PROJECT_ROOT)
