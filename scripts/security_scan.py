"""Fail when deployable source contains a credential-shaped literal or logs contain financial IDs."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "migrations",
)
SOURCE_FILES = (
    PROJECT_ROOT / "Dockerfile",
    PROJECT_ROOT / "compose.yaml",
    PROJECT_ROOT / "pyproject.toml",
    PROJECT_ROOT / "alembic.ini",
    PROJECT_ROOT / ".env.example",
)
TEXT_SUFFIXES = {".py", ".toml", ".ini", ".yaml", ".yml", ".example"}

CREDENTIAL_PATTERNS = {
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "openrouter_key": re.compile(r"\bsk-or-v1-[0-9A-Za-z_-]{20,}\b"),
    "telegram_bot_token": re.compile(r"\b\d{6,12}:[0-9A-Za-z_-]{30,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
FINANCIAL_LOG_PATTERNS = {
    "iranian_iban": re.compile(r"\bIR\s*\d(?:[\s-]*\d){23}\b", re.IGNORECASE),
    "card_number": re.compile(r"(?<!\d)\d{4}(?:[\s-]?\d{4}){3}(?!\d)"),
}


def _source_paths() -> list[Path]:
    paths = [path for path in SOURCE_FILES if path.is_file()]
    for root in SOURCE_ROOTS:
        if root.is_dir():
            paths.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and (path.suffix in TEXT_SUFFIXES or path.name == "Dockerfile")
            )
    return sorted(set(paths))


def scan(paths: list[Path], patterns: dict[str, re.Pattern[str]]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for name, pattern in patterns.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(PROJECT_ROOT)}: {name}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--logs",
        type=Path,
        default=PROJECT_ROOT / "logs",
        help="optional routine-log directory to scan",
    )
    args = parser.parse_args()
    findings = scan(_source_paths(), CREDENTIAL_PATTERNS)
    log_paths = list(args.logs.rglob("*")) if args.logs.is_dir() else []
    findings.extend(scan([path for path in log_paths if path.is_file()], FINANCIAL_LOG_PATTERNS))
    if findings:
        for finding in findings:
            print(f"FAILED: {finding}")
        return 1
    print(
        f"security scan passed: {len(_source_paths())} deployable source files and "
        f"{sum(path.is_file() for path in log_paths)} routine log files checked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
