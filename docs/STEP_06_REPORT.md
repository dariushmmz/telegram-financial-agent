# Step 06 Report — Safe Receipt Image Cleanup

## Status

**Complete and tested on 2026-10-21.** This execution implemented only Step 06. Step 07 was not started.

The cleanup workflow removes only eligible receipt image files from managed storage. It preserves receipt metadata, extraction history, financial requests, payments, allocations, reviews, corrections, report records, source references, and audit history.

## Implemented changes

### Cleanup domain service

Added `src/receipt_bot/cleanup/service.py` and its package exports. `ReceiptCleanupService` now:

- calculates a group-scoped cleanup preview using that group's configured retention period;
- uses the existing default retention of 183 days when the group has not changed it;
- includes only receipt images whose storage state is `present`, whose managed key exists in metadata, and whose creation time is at or before the cutoff;
- excludes a receipt while any associated extraction job is nonterminal;
- reports the eligible image count and metadata-estimated storage bytes;
- fingerprints the exact eligible record snapshot so a changed candidate set invalidates the confirmation;
- re-evaluates the preview before both final confirmation and execution;
- handles each file independently so one missing or invalid file does not prevent safe candidates from being processed;
- records `deleted`, `missing`, or `delete_failed` storage outcomes without deleting the receipt database row or its storage key;
- writes a group-scoped cleanup audit event containing the retention/cutoff and result counts.

Terminal extraction-job states are `succeeded`, `duplicate_exact`, and `failed_final`. A receipt with an `extracting`, queued, or otherwise nonterminal job is not eligible.

### Managed storage protections

Extended `ManagedStorage` with a deletion operation restricted to a validated POSIX-relative managed-storage key. It rejects:

- absolute paths;
- `..` traversal components;
- Windows-style backslash paths;
- symlinked parent components;
- symlink targets;
- directories and other non-regular targets.

The operation never follows a symlink during target classification. A missing managed file is reported as missing rather than treated as a fatal batch error.

### Telegram workflow

Implemented `/cleanup` as an administrator-only workflow:

1. `/cleanup` performs a live server-side administrator check and displays the current cutoff, eligible count, and estimated bytes.
2. `ادامه و بررسی نهایی` refreshes the preview and creates a new short-lived final-confirmation interaction.
3. The final delete button performs another live authorization check and eligibility refresh before deletion.
4. The result identifies successfully deleted, already-missing, and failed/unsafe files.

Both callback stages enforce server-side authorization. Demoting the initiating administrator before final execution prevents deletion. Accounting reviewers who are not group administrators cannot preview or execute cleanup. Callback tokens are actor-bound, group-bound, expiring, and consumed to prevent replay.

Persian messages explicitly state that image-file deletion is permanent while accounting and extraction records remain. Receipt/payment detail screens also indicate when an original image was removed under the retention policy.

### Application wiring

The Telegram application now injects the configured `ManagedStorage` instance into `TelegramUpdateHandler`, which constructs the cleanup service. If managed storage is unavailable, `/cleanup` fails closed with a Persian unavailable message.

### Database and migrations

No new migration was required. The existing schema already contains receipt storage state, cleanup timestamp, cleanup error, group retention configuration, interaction state, and audit-event support. The migration head remains `b6d91f4a2c38`.

## Acceptance results

| Acceptance criterion | Result | Evidence |
|---|---|---|
| Only group administrators can preview and execute cleanup | Passed | Command and callback authorization tests, including demotion before execution |
| Default/configurable age cutoff and group isolation | Passed | Cleanup preview test covers 183 days, fresh files, another group, and terminal-job eligibility |
| Unknown, traversing, symlinked, directory, and outside-root targets are not deleted | Passed | Managed-storage failure and symlink tests; outside target remains intact |
| Confirmation is tied to a fresh preview | Passed | Candidate-set change invalidates the snapshot before deletion |
| Explicit two-stage confirmation is required | Passed | Telegram test verifies that neither preview nor intermediate confirmation deletes a file |
| Replay cannot repeat a material action | Passed | Consumed final callback returns the expired-interaction response |
| Missing and per-file failures are recoverable | Passed | Mixed valid/missing/unsafe/directory batch completes with independent result states |
| Accounting and extraction data survive cleanup | Passed | Invariant test compares all seeded financial/extraction row counts and pre-existing audit rows before and after deletion |
| UI shows cutoff, count, size, permanence, and preservation warning | Passed | Telegram workflow assertions and Persian receipt-detail assertion |

## Automated validation

Commands used:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q tests/test_cleanup.py
python -m pytest -q
python -m compileall -q src tests
ruff check src/receipt_bot/cleanup src/receipt_bot/storage.py src/receipt_bot/telegram/handler.py src/receipt_bot/telegram/app.py src/receipt_bot/telegram/financial.py src/receipt_bot/telegram/texts.py tests/test_cleanup.py
```

Results:

- Cleanup-specific suite: **7 passed**.
- Full automated suite: **143 passed in 35.60 seconds** in the final verification run.
- Python bytecode compilation: **passed**.
- Ruff static checks for all Step 06 files: **passed**.

All cleanup tests use isolated temporary databases and storage roots. No cleanup command or deletion service was executed against the active project database or receipt storage.

## Data-preservation verification

The main invariant test creates a complete connected record set containing a Telegram group and source message, receipt metadata, payment evidence, extraction attempt, counterparty, payment request, payment, allocation, accounting review, correction/revision, report run, and audit events. After cleanup:

- the eligible image file is absent;
- its receipt row remains and is marked `deleted` with a cleanup time;
- its original managed storage key remains for traceability;
- all financial, extraction, review, revision, allocation, and report rows remain unchanged;
- every pre-existing audit row remains unchanged;
- one new cleanup execution audit event is present.

## Known limitations and operational notes

- Filesystem deletion and SQLite metadata updates cannot participate in one atomic transaction. If the process stops after unlinking a file but before committing its database state, a later cleanup pass safely classifies that record as `missing`; it cannot restore the file.
- Displayed storage savings use the receipt's stored byte-size metadata. This is an estimate and may differ from actual filesystem allocation.
- Retention is expressed as a fixed number of days; 183 days approximates six months rather than using calendar-month arithmetic.
- Empty parent directories are not removed. Cleanup concerns receipt files only.
- Unsafe or failed targets are retained as `delete_failed` metadata for operator investigation; the service does not automatically rewrite paths or retry permission failures.
- The workflow assumes the planned single active bot instance. Multi-instance serialization is outside the seven-day MVP.
- Live Telegram staging was not performed during this milestone. Automated handler tests exercised the command/callback workflow with isolated storage.
- A broader MVP gap remains: Telegram-side creation/classification of payment requests, counterparties, and promises still needs resolution before final acceptance. This is not part of the Step 06 cleanup scope.

## Next milestone

Step 07 is deployment and reliability hardening: persistent production runtime configuration, startup/health behavior, graceful shutdown, job recovery, safe backup/restore, log redaction/rotation, operator documentation, and final isolated staging acceptance.

Before declaring the whole MVP accepted, Step 07 must also address or explicitly schedule the outstanding Telegram ledger-creation gap described above. Proceed only after explicit authorization.
