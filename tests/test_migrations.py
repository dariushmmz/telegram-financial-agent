from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from conftest import alembic_config, sqlite_url


EXPECTED_TABLES = {
    "alembic_version",
    "audit_events",
    "counterparties",
    "extraction_attempts",
    "group_memberships",
    "interaction_sessions",
    "payment_promises",
    "payment_evidence",
    "payment_requests",
    "payments",
    "receipt_images",
    "receipt_jobs",
    "record_revisions",
    "report_runs",
    "request_payment_allocations",
    "reviews",
    "telegram_groups",
    "telegram_messages",
    "telegram_updates",
    "telegram_users",
    "text_receipt_jobs",
}


def test_clean_database_migrates_downgrades_and_recreates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("RECEIPT_BOT_DATABASE_URL", raising=False)
    url = sqlite_url(tmp_path / "migration.db")
    config = alembic_config(url)

    command.upgrade(config, "head")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
    trigger_names = set(
        engine.connect().execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars()
    )
    assert trigger_names == {
        "trg_allocation_insert_guard",
        "trg_allocation_date_insert_guard",
        "trg_allocation_date_update_guard",
        "trg_allocation_update_guard",
        "trg_audit_events_no_delete",
        "trg_audit_events_no_update",
        "trg_extraction_attempts_no_delete",
        "trg_extraction_attempts_no_update",
        "trg_record_revisions_no_delete",
        "trg_record_revisions_no_update",
        "trg_payment_allocation_guard",
        "trg_request_allocation_guard",
    }
    engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
    engine.dispose()


def test_migration_matches_model_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("RECEIPT_BOT_DATABASE_URL", raising=False)
    config = alembic_config(sqlite_url(tmp_path / "check.db"))
    command.upgrade(config, "head")
    command.check(config)


def test_existing_payments_migrate_as_legacy_without_amount_rewrite(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("RECEIPT_BOT_DATABASE_URL", raising=False)
    url = sqlite_url(tmp_path / "legacy.db")
    config = alembic_config(url)
    command.upgrade(config, "c4a81e2f9d10")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO telegram_groups "
                "(id, telegram_chat_id, title, active, timezone, retention_days, settings, settings_version, created_at, updated_at) "
                "VALUES (1, -10099, 'legacy', 1, 'Asia/Tehran', 183, '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO payments "
                "(id, group_id, amount_value, amount_unit, amount_rial, evidence_state, settlement_state, revision_number, created_at, updated_at) "
                "VALUES (1, 1, 12345, 'rial', 12345, 'submitted', 'not_checked', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT amount_rial, date_eligibility_state, parsed_transaction_at, date_max_age_days "
                "FROM payments WHERE id=1"
            )
        ).one()
    engine.dispose()
    assert tuple(row) == (12345, "legacy_unassessed", None, None)
