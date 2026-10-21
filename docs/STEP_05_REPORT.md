# Step 05 Report — Telegram Commands and Interactive Accounting Workflows

## Status

**Complete and deterministically tested.** This execution implemented Step 05 only. Receipt-file cleanup and deployment hardening were not implemented.

- Date: 2026-10-21
- Milestone: Step 05 / Day 5
- Final automated result after corrective reason-input fix: **136 passed, 0 failed, 0 skipped**
- Step 05 workflow tests: **20 passed, 0 failed, 0 skipped**
- Database migration: none required
- Current migration head: `b6d91f4a2c38`
- Live Telegram staging: not performed in this step
- Live Gemini/OpenRouter calls: not required or performed
- Next milestone: Step 06 / Day 6 — safe receipt image cleanup
- Gate: stop after this report and await explicit authorization.

## Commands implemented

All financial commands default to the current authorized group and require a current Telegram administrator or an active group accountant who is still a current group member.

| Command | Implemented behavior |
|---|---|
| `/reports` | Produces today's complete reconciliation report by default, records a traceable report run, and provides today/previous/next/7-day/30-day navigation. |
| `/summary` | Shows requested, submitted, confirmed, unverified, comparable difference, promise, and unresolved totals without merging their meanings. |
| `/pending` | Paginates unreviewed receipt evidence and proposed allocations. |
| `/verify` | Opens the same pending queue and then the selected payment/allocation review workflow. |
| `/receipts` | Paginates receipt candidates for a period with amount, evidence status, extraction status, evidence type, and image-storage state. |
| `/transactions` | Browses requests, receipts, promises, and active request–receipt associations with stable pagination and receipt drill-down. |
| `/balance` | Selects a group counterparty through inline buttons and shows requested, allocated, confirmed, unverified, unallocated, promise, and documentary-difference values with an explicit reliability warning. |

`/help` now documents all Step 05 commands and their authorization boundary.

## Interaction and authorization design

- Financial commands and every subsequent callback re-check live administrator/accountant authorization server-side.
- Callback sessions remain opaque, short-lived, bound to the initiating internal user and group, and constrained to Telegram's callback-data size.
- Forged use by another user resolves as invalid without exposing whether a target record exists.
- A reviewer who leaves the group or loses authorization cannot finish an already-open review operation.
- Pagination uses stable database IDs and stored page state rather than trusting user-supplied record data.
- Period navigation uses Tehran midnight boundaries. Commands default to today and inline buttons avoid complicated date syntax.
- An optional typed date accepts normalized Persian/English digits but must be an explicit Gregorian `YYYY-MM-DD` between 2000 and 2100. Jalali-looking input is rejected instead of silently interpreted as Gregorian.
- Long reports are split below Telegram's message limit; navigation markup is attached to the final part.

## Review, correction, and allocation workflows

The selected receipt screen supports:

- Confirm documentary evidence.
- Reject evidence.
- Correct its amount and unit.
- Select a compatible open request and create a proposed full or partial allocation.
- Repeat allocation to split one receipt across multiple requests without exceeding either side.
- Select a canonical receipt and mark another receipt duplicate.
- Explicitly record independently verified bank settlement, only after documentary evidence confirmation.

Proposed allocations can be confirmed or cancelled through the pending queue. Confirmation requires confirmed payment evidence.

Every material operation follows three stages:

1. Inline selection of the target and operation.
2. A reply prompt for a required reason, or amount/unit plus reason.
3. An inline final confirmation showing the intended operation.

The final callback consumes its interaction token in the same database transaction as the ledger mutation. Retrying that token cannot append a second review, revision, allocation, or audit event. Corrections and decisions retain original extractions and append revisions/reviews/audits.

### Corrective extension: accepting the review reason

The initial Step 05 handler only recognized a reason when Telegram included an explicit `reply_to_message` reference to the bot's force-reply prompt. In real use, sending the description as a normal next group message could therefore produce no response.

The handler now accepts either:

- An explicit reply to the current prompt; or
- The reviewer's next plain-text message while that reviewer has a current pending financial input in the same group.

Starting a newer input prompt consumes any older unfinished input prompt for the same reviewer and group, so a later plain message cannot be applied to the wrong abandoned operation. User-facing prompt text now explains both input methods. Authorization is still rechecked before accepting the reason, and the reason still leads to a separate final-confirmation button rather than applying the financial decision immediately.

## Privacy and traceability

- Public group receipt/report output excludes transaction references, account/card/IBAN values, and raw extraction details.
- When `TELEGRAM_BOT_USERNAME` is configured, authorized users receive short-lived private handoff links for receipt details and report sources.
- Private receipt detail can show whitelisted normalized extraction fields, printed transaction date, transaction reference, image state, and exact source-message navigation.
- Private report trace output includes request sources, allocated receipt sources, and unallocated receipt sources.
- Private handoffs are actor-bound, group-bound, single-use, and re-authorize current group access before showing data.
- ASCII digits inside Telegram source URLs are preserved so links remain valid; presentation-only numbers retain Persian formatting.

## Financial safeguards retained

- Telegram handlers call the Step 04 deterministic ledger and reporting services; they do not calculate money independently.
- All amounts remain exact integer rial values internally and are rendered as exact toman values.
- Submitted evidence, accounting confirmation, allocation confirmation, promises, and externally confirmed settlement remain distinct.
- A receipt is never automatically linked merely because its amount or person appears similar.
- The request picker only shows currently compatible, group-scoped records; a human still selects the request and allocation amount.
- Promises never enter submitted/confirmed totals.
- Duplicate and rejected receipts remain non-countable.
- `/balance` explicitly says it is neither confirmed debt nor a bank balance and warns when unknown request amounts exist.

## Automated validation

Step 05 tests cover:

- Unauthorized rejection for each of the seven financial commands.
- Access for an active accountant for each financial command.
- Default-today reports, report-run persistence, period navigation, and invalid typed dates.
- Summary, receipt, transaction, pending, verification, and balance output contracts.
- Stable receipt pagination and actor-bound callbacks.
- Public/private sensitive-field separation and single-use private handoffs.
- Private Telegram source links without digit corruption.
- Report source tracing for requests and receipts.
- Payment confirmation and allocation confirmation as separate actions.
- Explicit settlement confirmation remaining separate from receipt confirmation.
- Final-callback retry idempotency.
- Review reason entry both as an explicit reply and as the reviewer's next plain message.
- Amount correction with original revision preservation.
- Duplicate selection with a canonical payment and required reason.
- Split allocation across two requests without overcounting.
- Rejection reason persistence and non-countability.
- Reviewer revocation between workflow steps.
- Request–receipt associations in transaction browsing.
- Existing Step 01–04 extraction, persistence, authorization, reconciliation, and reporting regressions.

## Commands and results

Focused Step 05 suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_telegram_financial_workflows.py -q
```

Result after the corrective extension: **20 passed in 9.71s**.

Static checks for the new handler/workflow/test files:

```powershell
ruff check src/receipt_bot/telegram/financial.py src/receipt_bot/telegram/handler.py tests/test_telegram_financial_workflows.py
```

Result: **All checks passed**.

Complete deterministic suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q
```

Result after the corrective extension: **136 passed in 30.99s**.

## Limitations and pending work

- No live Telegram group was used in this step. Button rendering, deep links, privacy-mode behavior, and Bot API delivery remain staging checks rather than verified live behavior.
- Automated workflows use existing ledger requests, counterparties, and promises. Telegram-side creation/classification of new payment requests and promises from free-form conversation is not yet exposed as a reviewed user workflow; this remains an overall MVP gap that must be resolved before final acceptance.
- Typed historical dates are Gregorian only. Inline day/range buttons are the supported no-ambiguity path for Persian users; Jalali calendar entry/conversion is not implemented.
- Counterparty selection is based on existing group-scoped counterparty records. Alias merging and creating a counterparty from a Telegram user are not exposed in Step 05.
- Private-detail buttons require the configured bot username. When it is absent, sensitive fields remain hidden rather than falling back to public output.
- Transaction/request/promise browsing is read-only except where receipt review and allocation actions are available. Request and promise correction screens are not exposed through Telegram in this step.
- Telegram message output uses new messages rather than editing old workflow screens; expired older buttons fail safely.
- `/cleanup` is intentionally absent until Step 06.

## Next milestone

Step 06 should implement administrator-only receipt-image cleanup with previewed file count/bytes, a fresh confirmation token, strict managed-root validation, safe partial failure handling, and tests proving that no accounting, extraction, report, revision, or audit record is deleted.

No Step 06 functionality was implemented in this execution.
