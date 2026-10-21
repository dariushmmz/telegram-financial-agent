from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from receipt_bot.persistence.database import Database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RECEIPT_BOT_DATABASE_URL", raising=False)
    url = sqlite_url(tmp_path / "test.db")
    command.upgrade(alembic_config(url), "head")
    database = Database(url)
    try:
        yield database
    finally:
        database.dispose()
