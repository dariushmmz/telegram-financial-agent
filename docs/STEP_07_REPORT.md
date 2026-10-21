# Step 07 Report — Deployment, Reliability Hardening, and MVP Acceptance

## Status

**Engineering implementation complete and deterministically tested on 2026-10-21.** This execution implemented only Step 07 and completed the seven planned engineering milestones.

Production acceptance is **conditional, not yet signed off**. The isolated real-Telegram staging checklist still requires a dedicated test group and human accounting review, the exposed development Telegram/Gemini credentials must be rotated, and OpenRouter live transport remains pending because no OpenRouter key is available. No unperformed live check is reported as passed.

## Delivered changes

### Production runtime

- Added a non-root Python 3.12 `Dockerfile` with a health check, `SIGTERM`, deterministic dependency input, and runtime UID/GID 10001.
- Added `compose.yaml` for one bot replica, a persistent `receipt-data` volume, a separately mounted backup directory, a 45-second stop grace period, and bounded Docker log rotation.
- Added `.dockerignore`; `.env`, databases, runtime data, debug artifacts, samples, tests, documentation, and backups are excluded from image build context as appropriate.
- Compose forwards only explicitly named application settings and credentials, rather than every local `.env` entry.
- The application now creates managed storage, checks write access, runs Alembic to head, validates SQLite integrity/foreign keys/revision, and only then starts Telegram polling.

### Health, shutdown, and recovery

- Added `receipt_bot.operations` with startup preparation, migration-head validation, read-only diagnostics, online backup, and safe restore operations.
- Added configurable `RECEIPT_BOT_SHUTDOWN_GRACE_SECONDS` with a 30-second default.
- `SIGINT`/`SIGTERM` now set the polling stop event. Polling is cancelled promptly while an in-flight worker is allowed to finish within the grace period.
- Startup recovery immediately requeues abandoned `downloading`/`extracting` image and text jobs under the documented single-instance assumption.
- Existing durable Telegram update recovery, source/update/file/hash idempotency, and ledger materialization run at startup.

### Backup and restore

Added `scripts/manage_runtime.py` with:

- `prepare`: create storage, migrate, and health-check;
- `migrate`: upgrade to Alembic head;
- `check`: validate database integrity, foreign keys, revision, and storage presence;
- `backup DESTINATION`: use SQLite's online backup API, validate the temporary database, calculate SHA-256, and atomically publish without overwriting;
- `restore BACKUP DESTINATION`: validate the source, restore to a new path, validate again, and refuse any overwrite.

Automated restoration into a separate database preserved a deterministic report projection, its manifest, a payment request, and the recorded report run. This specifically exercises WAL-safe SQLite backup behavior instead of copying a live database file blindly.

### Logging and security

- Structured JSON logging now sanitizes Telegram Bot API URL tokens, bearer credentials, and common environment-style API-key/password values in both messages and exception text.
- Sensitive context-key redaction and HTTP transport-log suppression remain enforced.
- Added `scripts/security_scan.py` to inspect deployable source for credential-shaped literals and optional routine log files for Iranian IBAN/card-number patterns.
- Added explicit guidance that rendered `docker compose config` output can expand secrets and should not be captured.

### Final ledger-intake acceptance repair

The final audit confirmed the earlier documented gap: requests and promises existed in the ledger but could not be created through Telegram. Step 07 closes that gap conservatively:

1. An authorized administrator/accountant replies to the original participant message with `/request` or `/promise`.
2. The bot prompts for `amount unit | note` and shows an inline final confirmation.
3. The record uses the captured original message time, sender, group, and internal source reference.
4. The sender becomes a group-scoped counterparty only at final confirmation.
5. A source already classified as either a request or promise is rejected on preview and checked again at commit.
6. Promises remain separate from submitted/confirmed payment totals and the result explicitly says they are not payment.

Authorization is checked before starting the workflow and again through the actor/group-bound financial interaction. No AI classification, participant association, amount, or unit is invented.

### Operations documentation

Added:

- root `README.md` with local and Docker quick starts;
- `docs/OPERATIONS.md` covering Linux deployment, BotFather privacy mode, least privilege, onboarding, secrets/proxies, provider costs, health, log rotation, backup/restore, retention/privacy, and incident recovery;
- `docs/STAGING_ACCEPTANCE.md` with an evidence-oriented, synthetic-data Telegram staging checklist.

## Automated and build validation

Final commands included:

```powershell
python -m compileall -q src scripts tests
ruff check <all Step 07 modified Python files>
python scripts/security_scan.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
docker compose config --quiet
docker build --build-arg HTTP_PROXY=http://host.docker.internal:10808 `
  --build-arg HTTPS_PROXY=http://host.docker.internal:10808 `
  --tag receipt-bot-step07 .
```

Results:

- Full automated suite: **152 passed in 31.70 seconds** in the final verification run.
- Step 07 focused operations/logging/polling tests: passed.
- Python compilation: passed.
- Ruff checks for all Step 07 modified Python files: passed.
- Security scan: **63 deployable source files**, zero credential findings; no persistent routine log files existed to scan. Synthetic log-redaction tests passed.
- Compose validation: passed with `--quiet`.
- Linux container image build: passed.
- Container dependency check: `No broken requirements found`.
- Disposable Linux volume: fresh migration through all five revisions, health `ok`, revision/head `b6d91f4a2c38`, zero foreign-key violations; the exact test volume was removed afterward.
- Active project database read-only health: integrity `ok`, zero foreign-key violations, revision/head `b6d91f4a2c38`.
- No migration was added in Step 07; the existing schema supported all runtime and request/promise records.

The host-wide `pip check` reports conflicts in unrelated globally installed packages. The isolated production container reports no broken requirements and is the deployment acceptance environment.

## Live provider verification

One explicitly authorized direct-Gemini smoke call was performed through the configured proxy using `gemini-3.8-flash` and the approved `samples/sample.jpg` fixture.

- Exit code: zero.
- Provider/model response: present.
- Fixture SHA-256: `a928b3f5e70f7abb3dc7a0a97dad2acbf628d8dc8efb01379ffa2b91fce1292c`.
- `requires_review`: false for this fixture.
- Validation issue codes: none for this fixture.
- Output contained no extracted names, accounts, references, amounts, or credential.

This verifies one transport and structured-response path, not general OCR/handwriting accuracy. OpenRouter live verification remains pending because `OPENROUTER_API_KEY` is unavailable; its adapter contract tests are included in the passing automated suite.

## Final MVP acceptance matrix

| Criterion | Status | Evidence / limitation |
|---|---|---|
| Group-scoped capture of requests, promises, text/image receipts, senders, and source references | Automated pass; live pending | Reply-based creation and receipt intake/isolation tests pass; real Telegram staging checklist remains pending |
| Persian image/text extraction, uncertainty, rial/toman handling, Gemini/OpenRouter adapters | Pass with provider limitation | Deterministic validation/provider tests and direct Gemini smoke pass; OpenRouter live pending |
| Duplicate prevention across updates/messages/files/hashes/payments/promises | Passed | Database constraints and handler/worker/reconciliation tests |
| Authorized review, correction, rejection, duplicate, link/split, and immutable history | Passed automatically | Role/callback and accounting workflow tests; real Telegram UX staging pending |
| Deterministic `/reports` calculations and traceability | Passed | Golden/invariant reporting tests and backup/restore report projection comparison |
| `/summary`, `/pending`, `/verify`, `/receipts`, `/transactions`, `/balance` contracts | Passed automatically | Persian handler/workflow tests; client wording sign-off pending |
| Receipt/promise/difference disclaimers | Passed automatically | Rendering and Telegram workflow assertions |
| Safe `/cleanup` preservation guarantees | Passed | Seven cleanup-specific tests plus full regression suite |
| Fresh migration, health, restart recovery, backup/restore, redacted logs, runbook | Passed | Container build/disposable-volume run, operations tests, security scan, documentation |
| Isolated real-Telegram end-to-end staging | **Pending** | Requires credential rotation, dedicated test group, BotFather privacy configuration, and human tester |

## Credential incident and required action

During Step 07, a local `docker compose config` diagnostic expanded the development Telegram token and Gemini key into captured terminal output. Their values are intentionally not reproduced in any project file or report.

Before any further live/staging or production operation:

1. regenerate the Telegram bot token with BotFather;
2. revoke/regenerate the Google AI Studio Gemini key;
3. update the protected `.env` file;
4. restart the bot and verify `getMe`/Gemini using non-sensitive output;
5. review provider usage and any retained terminal/session logs.

The application code and deployment image contain no populated credentials. `.env` remains excluded from build context and source control.

## Remaining limitations

- Production sign-off requires every applicable item in `docs/STAGING_ACCEPTANCE.md`; it was not possible to simulate BotFather settings or human accounting approval in automated tests.
- The supported topology is one bot process and local persistent SQLite/WAL storage. Horizontal scaling is out of scope.
- Online database backup does not include receipt image files. A complete disaster-recovery set also needs a coordinated `/app/data/receipts` snapshot.
- Backup and restore cannot prove off-host storage durability; the operator must configure encrypted replication and scheduled restore rehearsals.
- The security scanner checks known credential shapes and financial identifiers; it is defense in depth, not proof that arbitrary prose contains no personal information.
- Container health validates persistence/schema/storage, while Docker process state detects a dead bot. It does not call Telegram or consume provider credit every 30 seconds.
- Retention is a fixed day count (183 by default), not calendar-month arithmetic.
- Pre-bot Telegram history cannot be recovered automatically.
- Stored sample/debug artifacts remain locally sensitive and excluded from deployment; owner authorization is still required before source-control publication.

## Conclusion

All seven planned engineering milestones are implemented. The deterministic MVP and deployment artifacts pass automated, backup/restore, container-build, fresh-volume, security, and direct-Gemini checks. The system is **not yet production-signed-off** until credentials are rotated and the isolated Telegram staging checklist is completed and accepted by the accounting/operator representatives.
