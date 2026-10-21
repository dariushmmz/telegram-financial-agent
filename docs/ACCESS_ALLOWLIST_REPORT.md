# Telegram Access Allowlist and Support Button Report

Date: 2026-10-21
Status: implemented; automated verification complete; isolated live Telegram staging pending

## Scope

This post-MVP hardening change restricts use of configured Gemini/OpenRouter credentials to explicitly approved Telegram groups while preserving the existing group administrator/accountant model and financial isolation. It also adds a Persian support button linking directly to the bot support contact and developer, `@amixrmz`.

No reconciliation, report-calculation, receipt-validation, or accounting-state logic was changed.

## Implementation

- Added required `RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES` configuration. It accepts comma-separated ASCII Telegram usernames, strips an optional leading `@`, compares case-insensitively, and rejects missing, empty, malformed, or normalized duplicate entries.
- Added one immutable runtime access policy shared by the Telegram handler, authorization services, and extraction worker.
- Group activation now requires both an allowlisted actor and a live Telegram `creator`/`administrator` result from `getChatMember`.
- A successfully activated group stores the normalized username as its authorization anchor and the numeric activating user ID for audit traceability in the existing versioned settings JSON. No database migration was necessary.
- A group remains authorized only while it is active and its stored authorizer username remains in the current runtime allowlist. Removing that username therefore revokes the group after restart without altering its financial records.
- Unauthorized group messages cannot create groups, users, updates, messages, receipt jobs, or text jobs. Unauthorized commands and callbacks receive a Persian access message and support link; ordinary receipt/chat noise is ignored to avoid public-group spam.
- Recovered callbacks are rechecked and discarded safely when authorization has been revoked.
- Recovered image/text jobs from a revoked group are marked final-failed with `group_unauthorized` before file download or provider invocation. Extraction-to-ledger materialization also skips unauthorized groups.
- Existing administrator/accountant checks remain in force after the group gate; approval does not grant accounting privileges to ordinary members.
- Added `پشتیبانی ربات` URL buttons to start/main, help, settings, and access-denied responses. The accompanying Persian text identifies [@amixrmz](https://t.me/amixrmz) as support and developer. Private `/start` and `/help` expose this assistance without registering the unauthorized user.
- Compose now requires the allowlist explicitly, and `.env.example`, README, operations, staging, and project-plan documentation describe its use and revocation behavior.

## Trust and rollout model

The allowlist is a bootstrap and ongoing group-approval control, not a replacement for group roles:

1. An operator adds a trusted Telegram username to the protected environment and restarts the service.
2. The account currently holding that username must also be an administrator of the intended Telegram group and run `/start` there.
3. All group members may then submit financial messages/receipts, while financial commands retain their current administrator/accountant authorization.
4. Removing the recorded authorizer from the allowlist and restarting blocks all processing for that group.
5. A different allowlisted current administrator may reactivate the same group; existing source, financial, extraction, report, and audit records remain intact.

Existing databases created before this change have no authorization marker. Their groups fail closed and must be reactivated through this procedure. This is intentional and does not require a destructive migration.

## Automated verification

Focused access, configuration, Telegram, worker, polling, cleanup, and financial workflow suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q tests/test_telegram_access_allowlist.py tests/test_config_storage_logging.py tests/test_telegram_handler.py tests/test_telegram_worker.py tests/test_text_receipts.py tests/test_telegram_polling.py tests/test_cleanup.py tests/test_telegram_financial_workflows.py tests/test_telegram_authorization.py
```

Result after the username revision: **84 passed**.

The focused tests verify allowlisted administrators, non-allowlisted administrators, allowlisted non-administrators, approved-group member intake, unauthorized groups, callback revalidation, private public support access, fail-closed configuration, and prevention of both file downloads and AI-provider calls for revoked recovered jobs.

Complete regression and engineering validation:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
python -m ruff check <all changed Python files>
python -m compileall -q src scripts tests
python scripts/security_scan.py
$env:RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES = '@amixrmz'
python scripts/manage_runtime.py check
docker compose config --quiet
```

- Full pytest suite: **170 passed in 36.88 seconds**.
- Ruff on every changed Python file: **passed**.
- Python compilation: **passed**.
- Security scan: **passed**, 65 deployable source files and zero routine log files checked.
- Runtime health: **passed**; SQLite integrity was `ok`, foreign-key violations were zero, and migration revision matched head `b6d91f4a2c38`.
- Compose with a configured allowlist: **passed**.
- Compose without an allowlist: **failed as intended** before service creation with the required-variable error.
- Updated production image build: **not run successfully** because Docker Desktop's Linux engine was unavailable on the validation machine. No container-build success is claimed for this change; the previous Step 07 image result predates it.

A repository-wide Ruff invocation also found 85 existing style findings in unchanged migrations/tests and an unrelated untracked documentation utility. Those files were not mechanically rewritten as part of this security-focused change. All Python files changed by this task pass Ruff.

## Limitations and pending live validation

- Telegram usernames are mutable and can potentially be reassigned. The Bot API cannot provide the same durable identity guarantee as a numeric-ID allowlist; this owner-requested tradeoff is explicitly documented.
- The numeric Telegram User ID is still stored with the authorization marker for audit traceability, but runtime allowlist matching uses the normalized username.
- Environment changes require a service restart because the allowlist is immutable for the lifetime of one process.
- Jobs rejected after revocation are final-failed rather than retained for automatic retry. An accountant/operator must deliberately resubmit evidence after reauthorization; this avoids unexpected future credential consumption.
- The support URL and inline-button payload are covered by deterministic tests, but opening the profile in real Telegram remains part of the isolated staging checklist.
- Live Telegram membership checks, real receipt delivery, and provider billing behavior have not been re-run for this hardening change. They must not be described as verified until the staging checklist is performed.

## Operator action required

Before the next run, add trusted Telegram usernames to `.env`, for example:

```dotenv
RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES=@amixrmz,backup_admin
```

Then restart the bot and have an allowlisted current administrator run `/start` in every approved group, including groups that were active before this change. Rotate the previously exposed development Telegram and Gemini credentials before staging, as already required by the Step 07 report.
