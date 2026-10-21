"""Migrate, validate, back up, and restore the production SQLite database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from receipt_bot.config import AppSettings
from receipt_bot.operations import (
    OperationError,
    backup_database,
    healthcheck,
    migrate_database,
    prepare_runtime,
    restore_database,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("prepare", help="create storage, migrate, and run health checks")
    subcommands.add_parser("migrate", help="upgrade the configured database to migration head")
    check = subcommands.add_parser("check", help="validate database, migration, and storage health")
    check.add_argument("--quiet", action="store_true")
    backup = subcommands.add_parser("backup", help="create a validated online SQLite backup")
    backup.add_argument("destination", type=Path)
    restore = subcommands.add_parser("restore", help="restore a validated backup to a new path")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "restore":
            result = restore_database(args.backup, args.destination)
            print(result.to_json())
            return 0
        settings = AppSettings()
        if args.command == "prepare":
            print(prepare_runtime(settings, root=PROJECT_ROOT).to_json())
        elif args.command == "migrate":
            migrate_database(settings.database_url, root=PROJECT_ROOT)
            print("database migration complete")
        elif args.command == "check":
            result = healthcheck(settings, root=PROJECT_ROOT)
            if not args.quiet:
                print(result.to_json())
        elif args.command == "backup":
            print(backup_database(settings.database_url, args.destination).to_json())
        return 0
    except (OperationError, OSError) as exc:
        print(f"operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
