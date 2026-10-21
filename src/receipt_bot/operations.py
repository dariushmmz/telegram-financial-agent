from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from receipt_bot.config import AppSettings
from receipt_bot.storage import ManagedStorage


class OperationError(RuntimeError):
    """A safe, operator-facing runtime or data-management failure."""


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    database_path: str
    revision: str
    migration_head: str
    integrity: str
    foreign_key_violations: int
    receipt_storage_path: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: str
    byte_size: int
    sha256: str
    revision: str
    integrity: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def sqlite_database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise OperationError("runtime operations require a file-backed SQLite database")
    return Path(url.database).expanduser().resolve(strict=False)


def _alembic_config(database_url: str, *, root: Path | None = None) -> Config:
    project = (root or repository_root()).resolve()
    ini_path = project / "alembic.ini"
    migrations_path = project / "migrations"
    if not ini_path.is_file() or not migrations_path.is_dir():
        raise OperationError("Alembic configuration or migrations directory is unavailable")
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def migration_head(*, root: Path | None = None) -> str:
    config = _alembic_config("sqlite:///unused.db", root=root)
    head = ScriptDirectory.from_config(config).get_current_head()
    if not head:
        raise OperationError("migration head is unavailable")
    return head


def migrate_database(database_url: str, *, root: Path | None = None) -> None:
    path = sqlite_database_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(database_url, root=root), "head")


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _database_diagnostics(path: Path) -> tuple[str, int, str]:
    if not path.is_file():
        raise OperationError(f"database file does not exist: {path}")
    try:
        with closing(_connect(path, readonly=True)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = "ok" if integrity_rows == [("ok",)] else "; ".join(
                str(row[0]) for row in integrity_rows
            )
            violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error as exc:
        raise OperationError(f"database diagnostics failed: {exc}") from exc
    if version_row is None or not version_row[0]:
        raise OperationError("database migration revision is unavailable")
    return integrity, violations, str(version_row[0])


def healthcheck(settings: AppSettings, *, root: Path | None = None) -> HealthReport:
    database_path = sqlite_database_path(settings.database_url)
    storage = ManagedStorage(settings.receipt_storage_root)
    integrity, violations, revision = _database_diagnostics(database_path)
    head = migration_head(root=root)
    problems: list[str] = []
    if integrity != "ok":
        problems.append(f"integrity={integrity}")
    if violations:
        problems.append(f"foreign_key_violations={violations}")
    if revision != head:
        problems.append(f"revision={revision}, expected={head}")
    if not storage.root.is_dir():
        problems.append("receipt storage directory is missing")
    if problems:
        raise OperationError("health check failed: " + "; ".join(problems))
    return HealthReport(
        status="ok",
        database_path=str(database_path),
        revision=revision,
        migration_head=head,
        integrity=integrity,
        foreign_key_violations=violations,
        receipt_storage_path=str(storage.root),
    )


def prepare_runtime(settings: AppSettings, *, root: Path | None = None) -> HealthReport:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    ManagedStorage(settings.receipt_storage_root).ensure_exists()
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".receipt-bot-write-check-",
            dir=settings.receipt_storage_root,
            delete=True,
        ) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        raise OperationError("receipt storage directory is not writable") from exc
    migrate_database(settings.database_url, root=root)
    return healthcheck(settings, root=root)


def _validate_backup_file(path: Path) -> tuple[str, str]:
    integrity, violations, revision = _database_diagnostics(path)
    if integrity != "ok" or violations:
        raise OperationError(
            f"backup validation failed: integrity={integrity}, foreign_key_violations={violations}"
        )
    return integrity, revision


def backup_database(database_url: str, destination: Path) -> BackupResult:
    source_path = sqlite_database_path(database_url)
    if not source_path.is_file():
        raise OperationError(f"database file does not exist: {source_path}")
    destination = destination.expanduser().resolve(strict=False)
    if destination == source_path:
        raise OperationError("backup destination must differ from the live database")
    if destination.exists():
        raise OperationError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(_connect(source_path, readonly=True)) as source, closing(
            _connect(temporary, readonly=False)
        ) as target:
            source.backup(target)
            target.commit()
        integrity, revision = _validate_backup_file(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        byte_size = temporary.stat().st_size
        os.replace(temporary, destination)
    except (OSError, sqlite3.Error) as exc:
        raise OperationError(f"database backup failed: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return BackupResult(
        path=str(destination),
        byte_size=byte_size,
        sha256=digest,
        revision=revision,
        integrity=integrity,
    )


def restore_database(backup_path: Path, destination: Path) -> BackupResult:
    backup_path = backup_path.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if backup_path == destination:
        raise OperationError("restore destination must differ from the backup")
    if destination.exists():
        raise OperationError("restore destination already exists; restore into an empty location")
    integrity, revision = _validate_backup_file(backup_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(_connect(backup_path, readonly=True)) as source, closing(
            _connect(temporary, readonly=False)
        ) as target:
            source.backup(target)
            target.commit()
        _validate_backup_file(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        byte_size = temporary.stat().st_size
        os.replace(temporary, destination)
    except (OSError, sqlite3.Error) as exc:
        raise OperationError(f"database restore failed: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return BackupResult(
        path=str(destination),
        byte_size=byte_size,
        sha256=digest,
        revision=revision,
        integrity=integrity,
    )
