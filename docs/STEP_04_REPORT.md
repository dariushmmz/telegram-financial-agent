# Step 04 Report — Reconciliation and Deterministic Reporting Engine

## Status

**Complete and deterministically tested.** This execution implemented Step 04 only. It did not add Telegram reporting, review, correction, or browsing commands; those remain Step 05.

- Date: 2026-10-21
- Milestone: Step 04 / Day 4
- Final automated result: **116 passed, 0 failed, 0 skipped**
- Targeted reconciliation/worker result: **23 passed, 0 failed, 0 skipped**
- Migration head: `b6d91f4a2c38`
- Active database: migrated successfully to head
- Pre-migration backup: `data/receipt_bot.pre_step04_20261021.db`
- Existing successful extractions materialized: 3 payment candidates; none was marked as settled by this operation
- Live Telegram or AI calls: not performed or required for this deterministic milestone
- Next milestone: Step 05 / Day 5 — Telegram reports, review, correction, and browsing workflows
- Gate: stop after this report and await explicit authorization.

## Changes implemented

### Group-scoped accounting ledger

- Added lifecycle services for counterparties, payment requests, payment evidence, payments, allocations, promises, reviews, corrections, duplicate decisions, and external-settlement assertions.
- Every lookup and mutation is scoped to one Telegram group. Cross-group identifiers fail instead of resolving another group's financial data.
- All monetary calculations use integer rial values. Unknown or incompatible units cannot enter comparable allocation totals.
- Submitted documentary evidence and independent bank settlement remain separate states. A receipt cannot set `externally_confirmed` by itself.
- Corrections append `record_revisions`, `reviews`, and `audit_events`; the original extraction remains immutable.

### Extraction-to-ledger materialization

- Successful image and Telegram-text extraction attempts now produce payment candidates through the common evidence layer.
- Extracted amounts, units, transaction dates, and reference identifiers are copied only from validated normalized fields.
- Validation issues or uncertain fields produce a `needs_review` payment instead of an invented or silently trusted value.
- Exact duplicate evidence points to its canonical payment and is non-countable.
- The receipt worker materializes new candidates immediately after successful extraction. Polling startup also backfills successful extractions that predate Step 04, idempotently.
- Payment creation time follows the original Telegram evidence submission time, preserving the report period basis.

### Allocations, matching, and duplicate safety

- One payment may be split across requests and one request may receive multiple payments through explicit allocation rows.
- Active allocation totals cannot exceed either the reliable payment amount or the request amount.
- Counterparty mismatch, closed/cancelled requests, rejected/duplicate payments, unknown units, and confirmation without reviewed evidence are rejected.
- Rejecting or marking a payment duplicate first rejects its active allocations, preventing stale amounts from remaining countable.
- Candidate matching is deterministic and read-only. A unique explicit reply can yield one unique suggestion; amount and counterparty similarity only narrow the candidate list. Multiple valid candidates remain explicitly ambiguous.
- Match suggestions never create allocations automatically.

### Database invariants and migration

- Added a canonical duplicate-payment link and explicit revision numbers for allocations and promises.
- Added composite group-aware foreign keys and positive revision checks.
- Added SQLite insert/update guard triggers for allocation and parent-record mutations. These preserve amount caps, status compatibility, unit comparability, counterparty consistency, and confirmation requirements even if application services are bypassed with direct SQL.
- Clean upgrade, downgrade, re-upgrade, model-drift, and trigger-presence tests pass.

### Deterministic reporting

- Added request-level projections for requested, submitted, confirmed, unverified, and comparable-difference values.
- Added separate sections for unallocated payment remainders and promises. Promises never reduce a request difference or enter submitted/confirmed totals.
- Duplicate and rejected payments contribute zero. Unknown-unit and otherwise unreliable records are exposed as exclusions/warnings rather than forced into totals.
- Overpayments are represented as the request's allocated amount plus an unresolved unallocated remainder, preventing double counting.
- Group aggregates are sums of the eligible displayed detail components.
- Every projection includes source-message references, record IDs, allocation/payment IDs, and active revision IDs. A canonical JSON manifest and SHA-256 digest make a report run traceable.
- Added Persian rendering with Persian digits, exact toman formatting, accounting-state labels, and explicit warnings that documentary receipts are not independent bank-settlement confirmation and request differences are not overall debt.
- Persisted report runs record the period, calculation version, manifest hash, and output hash.

## Acceptance validation

| Requirement | Evidence |
|---|---|
| Exact, partial, split, overpayment, and multiple-receipt calculations | Covered by reconciliation and reporting integration tests |
| Unallocated, cancelled, duplicate, rejected, promise-only, and unknown-unit cases | Covered with explicit zero-count/exclusion assertions |
| Ambiguous association | Matching returns `ambiguous`; no allocation is written |
| Corrected amounts and review history | Append-only revision/review/audit assertions pass |
| Date filtering | Start-inclusive/end-exclusive boundary tests pass |
| Submitted versus confirmed | Separate exact totals asserted; external settlement is independent |
| No double counting | Split/overpayment/duplicate tests and database caps pass |
| Aggregate accuracy | Aggregate fields are asserted against declared expected values and detail sums |
| Integer-only arithmetic | Property-style integer partition tests pass; no float money path is used |
| Traceability | Source messages, records, revisions, manifest, and report-run hashes are asserted |
| Persistence enforcement | Direct-SQL attempts to violate allocation invariants are rejected by database triggers |
| Existing extraction integration | Worker and startup materialization are idempotently tested |

The main report fixture asserts these exact rial totals:

- Requested: `17,000,000,000`
- Submitted: `17,039,000,000`
- Confirmed: `6,000,000,000`
- Unverified: `11,039,000,000`
- Comparable difference: `61,000,000`
- Promised, shown separately: `500,000,000`

## Commands and results

Targeted tests after startup materialization integration:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_reconciliation_service.py tests/test_telegram_worker.py tests/test_telegram_polling.py tests/test_text_receipts.py -q
```

Result: **23 passed in 9.07s**.

Complete deterministic suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q
```

Result: **116 passed in 21.35s**.

Database verification:

```powershell
alembic current
```

Result: `b6d91f4a2c38 (head)`.

The one-time local backfill found three successful extraction attempts, created three payment candidates, and changed the payment count from zero to three. Only counts were printed; financial contents were not logged.

## Issues encountered and resolved

- An early full run passed 114 tests before database-bypass guard cases were added. Two guard tests were then added and the complete hardened suite passed 116 tests.
- A timestamp assertion initially compared timezone-aware Python input to SQLite's timezone-naive returned value. The test now normalizes the SQLite value to UTC before comparing the same instant; no production timestamp or calculation was changed.
- The polling startup path initially recovered extraction jobs but did not materialize already-successful pre-Step-04 extraction rows. An idempotent startup materializer and integration tests now cover that upgrade path.

## Limitations and pending work

- Step 04 exposes application services and renderers, not Telegram command handlers. `/reports`, `/summary`, `/pending`, `/verify`, `/receipts`, `/transactions`, and `/balance` remain Step 05.
- The ledger enforces group isolation and financial invariants. It expects Step 05's server-side authorization layer to verify administrator/accountant permissions before calling reviewer mutations.
- A recorded report is reproducible by its current-state manifest and hashes; arbitrary reconstruction of every prior historical database state is not implemented.
- Similar name, amount, or time signals do not auto-link records. Ambiguous cases require an authorized human selection.
- Exact duplicate extraction evidence is preserved and linked to canonical evidence, but is not materialized as another countable payment.
- No live Telegram workflow was exercised in this step. No AI recognition claim was added or reverified.

## Next milestone

Step 05 should expose the tested engine through Telegram, prioritizing `/reports`, followed by summary, pending review, verification/correction, receipt/transaction browsing, and conservative balance views. It must re-check group authorization server-side for every command and callback and keep sensitive drill-down details in private interactions.

No Step 05 functionality was implemented in this execution.
