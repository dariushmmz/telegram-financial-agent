# Step 01 Report — Application Foundation, Domain Types, and Persistence

## Status

**Complete and tested.** This execution implemented Step 01 only. It did not implement AI extraction adapters, Telegram handlers, reconciliation calculations, reports, review workflows, or cleanup behavior.

- Date: 2026-10-20
- Milestone: Step 01 / Day 1
- Final deterministic test result: **35 passed, 0 failed, 0 skipped**
- Next milestone: Step 02 / Day 2 — Provider-independent receipt extraction
- Gate: stop after this report and await explicit authorization.

## Changes implemented

### Project and dependency foundation

- Added a `src/receipt_bot` package and pytest configuration in `pyproject.toml`.
- Declared Python 3.12–3.13 support and bounded runtime/test dependency ranges.
- Added exact Step 01 runtime and test lock files (`requirements.lock` and `requirements-test.lock`).
- Added `.gitignore` rules for credentials, databases/WAL files, managed financial data, logs, caches, virtual environments, and sensitive prototype debug artifacts.
- Added `.env.example` with placeholders only. The pre-existing `.env` was not read by the application, changed, or copied.
- Added structured JSON logging with recursive redaction for secret/token/API-key/password/authorization context fields.

### Validated configuration and managed storage

- Added Pydantic settings for the required SQLite URL and data root, plus log level, `Asia/Tehran`, 183-day retention, and sensitive-debug flag.
- Added masked optional placeholders for future Telegram, Gemini, and OpenRouter credentials without making those providers part of Step 01.
- Restricted the MVP database backend to SQLite and rejected filesystem-root data directories.
- Added a managed-storage resolver that accepts only non-empty relative POSIX keys and rejects absolute paths, traversal, Windows backslash paths, cross-drive paths, and symlink escapes outside the resolved root.

### Deterministic domain primitives

- Added enums for currency and the planned request, payment, extraction, storage, promise, settlement, membership, and review states.
- Added exact `Money` handling based only on integers:
  - rial remains unchanged;
  - toman converts to rial by exact multiplication by 10;
  - unknown units preserve their source value and deliberately have no canonical rial amount.
- Added Persian and Arabic-Indic digit normalization and strict non-negative integer parsing with common grouping separators.
- Added exact rial-to-toman formatting with Persian digits/group separators. Non-divisible rial amounts retain one exact decimal toman digit instead of being rounded.
- Added aware UTC/Tehran conversion and inclusive-start/exclusive-end Tehran day boundaries using the standard-library time-zone database.

### Database and migration foundation

- Added SQLAlchemy engine/session management with SQLite foreign keys, WAL mode, and a five-second busy timeout.
- Added Alembic configuration and initial revision `914ed51acf70`.
- Added the 16 planned application tables:
  - Telegram groups, users, memberships, and source messages;
  - counterparties, payment requests, receipt images, extraction attempts, payments, allocations, and promises;
  - reviews, record revisions, audit events, report runs, and interaction sessions.
- Added unique constraints for Telegram update/message idempotency and group/file receipt identity.
- Added group-aware composite foreign keys for tenant-sensitive relationships such as source messages, counterparties, receipts, requests, payments, promises, and allocations.
- Added non-negative/positive financial and revision constraints and period validity constraints.
- Added SQLite triggers that reject updates or deletes to record revisions and audit events. The repository exposes append/list methods only for these histories.
- Added short transaction/session handling that commits on success, rolls back on failure, and always closes the session.

### Repository boundaries

- Added repositories for groups, users, idempotent message ingestion, group-scoped financial constructors/allocations, and append-only history.
- Message ingestion first returns an existing group/message record and the database unique constraint remains the race-condition barrier.
- Composite foreign keys reject an allocation when the request/payment does not belong to the repository's group.

## Automated tests and acceptance evidence

The final suite was run with:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest
```

Result:

```text
collected 35 items
35 passed in 1.38s
```

Coverage of the Step 01 acceptance criteria:

| Acceptance criterion | Evidence |
|---|---|
| Clean database migrates from zero to head and can be recreated | A temporary database upgrades to head, verifies all 16 application tables and four append-only triggers, downgrades to base, then upgrades again. |
| Migration matches the SQLAlchemy schema | Alembic `command.check` reports no pending model/schema operations after upgrade. |
| Exact money and both Persian digit systems | Tests cover Persian/Arabic-Indic digits, separator parsing, rial, toman, unknown unit, invalid numbers, negative differences, large grouped output, and a rial value requiring fractional toman display. |
| Tehran time behavior | Tests assert aware inputs and the exact UTC boundaries for a Tehran calendar date. |
| Same message cannot be ingested twice | Repository replay returns the same row with `created=False`; a separate direct-insert test verifies the unique database barrier. |
| Cross-group relationships are rejected | A group-one request cannot allocate a group-two payment; SQLite raises a foreign-key integrity error. |
| Corrections/audits are append-only | Repository API has only append/list operations; database triggers reject direct SQL update/delete and tests verify the original rows remain unchanged. |
| Missing/invalid configuration fails safely | Tests cover missing required settings, unsupported DB backend, default retention, derived storage location, and masking/non-disclosure of a supplied token. |
| Managed storage cannot escape its root | Tests reject empty, absolute, traversal, Windows-style, and existing symlink escape paths while accepting a safe nested key. |
| Structured logs redact secrets | Nested token and API-key context values are replaced with `[REDACTED]` while safe correlation data remains. |

### Test-run history and fixes

The first plain `python -m pytest` invocation did not collect project tests because the workstation globally auto-loaded an unrelated `hydra` plugin whose installed ANTLR runtime is incompatible. This is outside the locked project dependency set. The deterministic command therefore disables unrelated third-party plugin auto-loading.

The first isolated project run collected all 35 tests: 33 passed and two migration tests failed. The tests exposed that the SQLite foreign-key PRAGMA in `migrations/env.py` opened an implicit SQLAlchemy transaction before Alembic's migration transaction. Schema DDL remained while the Alembic version insert rolled back. The environment now commits immediately after that PRAGMA, allowing Alembic to own and persist the migration/version transaction. The full isolated suite was rerun and all 35 tests passed.

No failure or blocked collection is being represented as a passing result; the status above is from the final rerun.

## Validation beyond pytest

- Imported SQLAlchemy metadata and confirmed all 16 application table definitions are registered.
- Alembic autogeneration identified the initial schema, after which the generated migration was reviewed and append-only triggers were added deliberately.
- The migration round-trip and model drift checks were executed against temporary databases, not the future production path.
- A final source/document scan verified that no value from the existing `.env` was copied into new project files.

No Telegram API, Gemini API, OpenRouter API, receipt OCR, or live financial workflow was invoked or verified in this milestone.

## Files added or changed

### Configuration and packaging

- `.gitignore`
- `.env.example`
- `pyproject.toml`
- `requirements.lock`
- `requirements-test.lock`
- `alembic.ini`

### Application foundation

- `src/receipt_bot/__init__.py`
- `src/receipt_bot/config.py`
- `src/receipt_bot/logging.py`
- `src/receipt_bot/storage.py`
- `src/receipt_bot/domain/__init__.py`
- `src/receipt_bot/domain/enums.py`
- `src/receipt_bot/domain/money.py`
- `src/receipt_bot/domain/time.py`
- `src/receipt_bot/persistence/__init__.py`
- `src/receipt_bot/persistence/database.py`
- `src/receipt_bot/persistence/models.py`
- `src/receipt_bot/persistence/repositories.py`

### Migrations and tests

- `migrations/README`
- `migrations/env.py`
- `migrations/script.py.mako`
- `migrations/versions/914ed51acf70_initial_schema.py`
- `tests/conftest.py`
- `tests/test_config_storage_logging.py`
- `tests/test_migrations.py`
- `tests/test_money.py`
- `tests/test_persistence.py`
- `tests/test_time.py`

### Progress documents

- Updated `docs/PROJECT_PLAN.md` to mark Step 01 complete with its tested result.
- Added `docs/STEP_01_REPORT.md`.

The existing `extract_receipt.py`, sample images/JSON, debug artifacts, and `.env` were not modified.

## Limitations and deferred work

- The foundation intentionally supports one SQLite-backed service instance. PostgreSQL and horizontal workers remain outside the MVP plan.
- Runtime SQLAlchemy writes use foreign keys/WAL/busy timeout. Backup and restart-recovery operating procedures remain Step 07.
- The exact lock files pin versions but do not yet include package hashes; deployment reproducibility/hardening is finalized in Step 07.
- Message idempotency has a database uniqueness barrier; Step 03 must translate a concurrent uniqueness race into a clean read/replay result at the Telegram intake boundary.
- Schema fields and lifecycle states exist, but Step 01 does not implement extraction, matching, reconciliation, reporting, authorization, or cleanup business services.
- Append-only enforcement is SQLite-specific because SQLite is the agreed MVP backend. A future database migration must implement equivalent protections.
- The existing sample/debug data may contain personal financial information and remains pending the owner decision recorded in the project plan.
- The globally installed pytest plugin conflict remains on the workstation, but it is not part of the project's locked environment. Test commands must keep plugin auto-loading disabled unless tests run in an isolated virtual environment.

## Next milestone

After explicit authorization, implement **Step 02 / Day 2 — Provider-independent receipt extraction** only:

- adapt reusable preprocessing and IBAN logic from `extract_receipt.py`;
- define neutral extraction, uncertainty, evidence, validation, and typed-error contracts;
- implement Gemini and OpenRouter adapters with credential-independent mock tests;
- validate image/content limits, numeral/unit/date/account/reference fields, and extraction persistence;
- make sensitive diagnostics opt-in and managed;
- document live Gemini status accurately and keep OpenRouter live testing pending until credentials exist.

Step 03 must not begin in that same execution.
