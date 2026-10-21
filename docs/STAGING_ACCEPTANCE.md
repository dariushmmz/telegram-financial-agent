# Isolated Telegram Staging Acceptance Checklist

Use a dedicated group and synthetic financial data. This checklist is an operator procedure, not evidence that a live check has already passed.

Record:

- Date/time:
- Tester:
- Container image digest or source version:
- Telegram bot username:
- Extraction provider/model:
- Database migration revision:

## Onboarding and authorization

- [ ] Bot privacy mode is disabled and the bot receives ordinary group messages.
- [ ] Missing, empty, malformed, or case-duplicate `RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES` prevents startup.
- [ ] A non-allowlisted current admin cannot register or activate a group.
- [ ] An allowlisted non-admin cannot register or activate a group.
- [ ] An allowlisted current group admin activates with `/start`.
- [ ] An ordinary member of the authorized group can submit a synthetic receipt.
- [ ] An unapproved group cannot enqueue work or cause a provider call.
- [ ] `/settings` adds an accountant; a normal member remains unauthorized.
- [ ] Demoting/revoking a reviewer prevents the next sensitive callback server-side.
- [ ] Removing the activating username from the runtime allowlist blocks existing commands, callbacks, receipt intake, recovered jobs, file downloads, and provider calls after restart.
- [ ] Start/help/settings and access-denied views show `پشتیبانی ربات`, open `https://t.me/amixrmz`, and identify `@amixrmz` as support and developer.

## Request, promise, and receipt intake

- [ ] Reply to a synthetic person's message with `/request`; enter amount/unit and confirm.
- [ ] The request shows the correct participant, amount, original Telegram source, and audit history.
- [ ] Repeating classification for the same source is rejected.
- [ ] Reply to another message with `/promise`; confirm it appears separately and is not counted as payment.
- [ ] Upload one printed Persian receipt image and one synthetic handwritten form.
- [ ] Send one supported Persian text receipt.
- [ ] Unsupported documents fail clearly without creating payment evidence.
- [ ] Re-send the same Telegram file/message and verify no double counting.

## Review and correction

- [ ] `/pending` and `/verify` show uncertain items.
- [ ] Confirm one receipt with a written reason.
- [ ] Correct one amount/unit with a written reason; original extraction remains visible.
- [ ] Reject one item and mark one duplicate.
- [ ] Link/split a payment only through explicit request selection and confirm the allocation.
- [ ] Private detail/source links open only for the initiating authorized user.

## Reporting

- [ ] `/reports` today matches independently calculated synthetic totals.
- [ ] Historical date and 7/30-day navigation use Tehran boundaries.
- [ ] `/summary`, `/receipts`, `/transactions`, and `/balance` agree with the report.
- [ ] Requested, submitted, confirmed, uncertain, promised, and unresolved values remain distinct.
- [ ] Warnings state that receipts do not prove settlement and request differences are not overall debt.
- [ ] Unknown-unit/ambiguous evidence produces an explicit reliability warning rather than an invented balance.

## Cleanup and restart

- [ ] `/cleanup` rejects accountants/non-admins.
- [ ] Preview shows cutoff, count, estimated bytes, and preservation warning.
- [ ] Changing eligibility invalidates an old confirmation.
- [ ] Final confirmation deletes only a synthetic eligible image; financial/detail history remains.
- [ ] Stop during a synthetic queued job, restart one instance, and verify recovery without duplicate records.

## Operations

- [ ] Container health is `healthy`; `manage_runtime.py check` reports current revision and zero violations.
- [ ] Routine logs contain no message bodies, account identifiers, image data, or credentials.
- [ ] Online backup succeeds and prints SHA-256.
- [ ] Restore into a separate database succeeds; a known historical report and manifest match.
- [ ] Graceful `docker compose stop` completes within the configured grace period.

## Sign-off

- [ ] Every failed or skipped item has an owner and disposition.
- [ ] Accounting representative accepts the Persian report wording and definitions.
- [ ] Operations owner accepts retention, off-host backup, restore, and incident procedures.
