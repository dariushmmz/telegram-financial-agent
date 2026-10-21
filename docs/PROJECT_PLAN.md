# Telegram Receipt & Financial Reconciliation System — Project Plan

## 1. Document control

This document is the authoritative implementation roadmap for the seven-day MVP. If scope, data rules, or milestone order changes, this file must be updated before implementation continues.

- Project interface: Telegram bot only; no web dashboard.
- Primary outcome: accurate, traceable financial reports for authorized Telegram groups.
- Base currency for exact arithmetic: integer Iranian rial; reports default to a precise toman representation.
- Time zone: `Asia/Tehran`; source dates retain their original calendar and text.
- MVP constraints: Python, lightweight persistent storage, Gemini first, OpenRouter-compatible adapter, no bank-settlement verification, no custom OCR training.
- Execution rule: exactly one milestone may be implemented in each authorized execution. Its tests and step report must be completed before work stops.

### Progress states

- `[ ]` not started
- `[~]` in progress; must not be used when an execution ends
- `[x]` complete and tested to the acceptance criteria recorded in its step report
- `[!]` blocked; the reason and evidence must be recorded in both this plan and the corresponding step report

### Milestone tracking

- [x] **Step 00 — Repository assessment and implementation plan** (2026-10-19; see `docs/STEP_00_REPORT.md`)
- [x] **Step 01 / Day 1 — Application foundation, domain types, and persistence** (2026-10-20; 35 tests passed; see `docs/STEP_01_REPORT.md`)
- [x] **Step 02 / Day 2 — Provider-independent receipt extraction** (2026-10-20; 73 tests passed; direct Gemini smoke test passed, OpenRouter live check pending; see `docs/STEP_02_REPORT.md`)
- [x] **Step 03 / Day 3 — Telegram intake, isolation, authorization, and basic commands** (2026-10-21 corrective extension complete; 100 tests passed; image and Telegram-text payment evidence share one traceable evidence layer; Telegram token rotation remains required before further staging; see `docs/STEP_03_REPORT.md`)
- [x] **Step 04 / Day 4 — Reconciliation and deterministic reporting engine** (2026-10-21; 116 tests passed; extraction candidates now materialize into a group-scoped ledger, matching remains conservative, and reports are deterministic and traceable; see `docs/STEP_04_REPORT.md`)
- [x] **Step 05 / Day 5 — Telegram reports, review, correction, and browsing workflows** (2026-10-21; corrective reason-input fix verified; 136 tests passed; all planned financial commands, private trace/detail handoffs, date navigation, pagination, and final-confirmed review/correction/link workflows implemented; see `docs/STEP_05_REPORT.md`)
- [x] **Step 06 / Day 6 — Safe receipt image cleanup** (2026-10-21; 143 tests passed, including 7 cleanup-specific tests; admin-only preview and two-stage confirmation, managed-path protections, stale-preview detection, partial-failure recovery, and accounting-record preservation verified; see `docs/STEP_06_REPORT.md`)
- [x] **Step 07 / Day 7 — Deployment, reliability hardening, and MVP acceptance** (2026-10-21; engineering implementation complete; 152 automated tests passed; Linux container build and disposable-volume fresh migration/health passed; validated SQLite backup/restore, graceful shutdown/recovery, credential redaction/scan, operations runbook, and reply-based `/request`/`/promise` capture implemented; direct Gemini smoke passed; isolated live Telegram staging and production sign-off remain explicitly pending; see `docs/STEP_07_REPORT.md`)
- [x] **Post-MVP hardening — Telegram username allowlist and support access** (2026-10-21; changed from numeric IDs to normalized Telegram usernames at owner request; 170 automated tests passed; unauthorized/revoked groups cannot enqueue or consume provider work; username mutability risk documented; updated container build and isolated live Telegram staging remain pending; see `docs/ACCESS_ALLOWLIST_REPORT.md`)

An unchecked milestone is not implemented, even if it is described here. A feature is verified only when its automated test or documented manual/live check has passed.

## 2. Repository assessment

### 2.1 Initial Step 00 contents (historical baseline)

The following records the repository as it was inspected on 2026-10-19, before implementation began. The current organized layout is summarized in the root `README.md`.

At that point, the repository was a prototype rather than an application:

- `extract_receipt.py`: a standalone Gemini receipt-extraction CLI.
- `samples/sample.jpg` through `samples/sample5.jpg`: local receipt-image samples.
- `samples/sample.json`: one extraction result.
- `debug/`: a sent-image copy and extraction log from one run, plus another sent-image artifact.
- `.env`: contains credential/configuration variable names, but there is no loader in the script.

There is currently no Git repository metadata, dependency manifest, test suite, package structure, database, migrations, Telegram implementation, README, `.gitignore`, deployment configuration, or CI configuration.

Environment inspected on 2026-10-19:

- Python 3.12.6
- `google-genai` 1.54.0
- `pydantic` 2.12.5
- `Pillow` 10.2.0

These installed versions describe the workstation only. Step 01 must pin supported ranges in a project manifest and lock/reproducibility mechanism.

### 2.2 Analysis of the original `extract_receipt.py`

The compatibility CLI now lives at `scripts/extract_receipt.py`; this section preserves the findings from the original root-level prototype.

#### Gemini integration

- Uses the official `google.genai` client with `GEMINI_API_KEY` read directly from the process environment.
- Does not load `.env`; credentials must be exported to the process environment by the operator/runtime. During the Step 02 verification follow-up, the ambiguous local `.env` name `API_KEY` was renamed to `GEMINI_API_KEY`, matching the application and `.env.example` contract.
- Sends a `types.Part` containing the image and a Persian-receipt prompt to `client.models.generate_content`.
- Requests JSON using `response_mime_type="application/json"` and the Pydantic `Receipt` class as `response_schema`.
- Sets temperature to zero.
- Tries hard-coded candidate model IDs in order, unless `--model` or `GEMINI_MODEL` is supplied.
- Retries HTTP 429/500/503 with exponential delays; it moves to another model for 404 and 429. Other API errors propagate.
- Supports listing models available to a key through `--list-models`.
- Parses `response.parsed`, falling back to Pydantic validation of `response.text`.

The existing debug artifact records a successful use of `gemini-3.8-flash`. That proves only the historical sample run; model availability remains an operational dependency and must not be assumed from this artifact.

#### Extraction schema

The current schema includes source account/name, destination IBAN/name, purpose, amount in rial, description, transaction date/time, tracking code, transfer status, sender note, and a Boolean indicating a non-final-payment warning. Most fields are nullable. The prompt asks the model to preserve Persian text and masked account characters, normalize numeric digits, retain the printed Jalali date, and return `null` rather than guess.

Only the destination IBAN receives deterministic post-validation (ISO 13616 mod-97). There is no validation for amount sign/range, date, card/account shape, tracking code, output consistency, or currency unit. There is no field-level confidence/evidence representation. In particular:

- `amount_rial` forces a rial interpretation even when a document says toman or the unit is unreadable.
- `has_not_final_payment_warning=False` conflates a confidently absent warning with an unreadable or unexamined warning.
- A structurally valid response may still contain OCR errors without being routed to review.
- Raw AI output is treated as the effective extraction, rather than retained as an immutable observation followed by a validated/reviewed revision.

#### Image preprocessing and supported formats

Reusable behavior:

- Reads original bytes and computes SHA-256.
- Uses Pillow for EXIF orientation correction, transparency flattening onto white, selected color-mode conversion, and optional grayscale/autocontrast/unsharp-mask processing.
- Downscales only when the longest side exceeds 4096 pixels and never upscales.
- Rejects an obviously uniform or nearly black processed result and falls back to original bytes.
- Encodes changed images as lossless PNG and otherwise sends the original bytes.
- Records original/sent dimensions, format, MIME type, hashes, steps, and warnings.

The CLI advertises JPG/PNG/WebP. In practice, there is no explicit format allowlist: acceptance depends on Pillow decoding, image mode, and inferred MIME type. PDF is unsupported, HEIC requires an unavailable plugin, and animated/multi-frame images are not deliberately handled. File size, pixel count, decompression-bomb behavior, corrupt/truncated files, and MIME/content mismatches are not converted into domain-level errors. These controls must be added before Telegram uploads are trusted.

#### Diagnostics and output

Every extraction writes the exact image sent to Gemini and a JSON log containing hashes, preprocessing metadata, raw model text, and parsed financial data. This is useful for prototype diagnosis but inappropriate as the production default because it duplicates sensitive receipt data and has no retention/access policy. Filenames have only second-level timestamp resolution, creating a collision risk under concurrency. Paths are local and unmanaged, errors use `sys.exit` inside reusable functions, timestamps are naive, and logs are not correlated to a group/message/extraction ID.

#### Reuse decision

Do not rewrite the proven image routines unnecessarily. In Step 02, move and adapt the conservative preprocessing, SHA-256 calculation, damage check, prompt intent, structured Gemini call, retry classification, and IBAN checksum into testable modules. Wrap providers behind an interface, strengthen validation and uncertainty modeling, replace process exits with typed exceptions, and make sensitive debug capture explicitly configured, access-controlled, and retention-bound.

### 2.3 Current implementation and remaining limitations

- Steps 01–07 implement the Telegram intake, group isolation and authorization, image/text receipt extraction, deterministic reconciliation/reporting, accounting review/correction, safe image cleanup, durable recovery, and single-instance deployment architecture described by this plan.
- Post-MVP access hardening adds a required environment allowlist of normalized Telegram usernames. Only an account whose username is allowlisted and which is also a current Telegram administrator may authorize a group. The stored username must remain in the runtime allowlist, so removal revokes all group processing—including recovered jobs—without deleting its financial data. The activating numeric ID is retained for audit, but usernames are mutable and therefore weaker authorization identifiers.
- Step 07 adds explicit reply-based `/request` and `/promise` capture so authorized reviewers can create traceable ledger records from original participant messages. The same source message cannot be classified repeatedly as a request or promise, and a promise remains separate from payment totals.
- Direct Gemini transport and structured extraction passed an opt-in live smoke test. The OpenRouter adapter is covered by deterministic mocked contract tests; its live smoke test remains pending because no OpenRouter credential is available.
- The isolated end-to-end Telegram staging checklist remains pending. Automated handler tests do not verify BotFather privacy mode, real Telegram file delivery, production group permissions, or client approval of Persian accounting wording.
- The Telegram token and Gemini key used during development must be rotated before production because a local Compose diagnostic expanded environment values into captured terminal output. No credential values are recorded in project documentation.
- The MVP intentionally supports one active process with SQLite/WAL. Horizontal workers, network filesystems, PostgreSQL, bank settlement verification, guaranteed handwriting recognition, and pre-bot Telegram history are unsupported.
- Image deletion and SQLite metadata updates cannot form one atomic filesystem transaction; restart/rerun recovery safely classifies an already removed file as missing but cannot restore it.
- Stored `samples/` and `debug/` artifacts are excluded from deployment builds and must be treated as sensitive until the owner confirms they are synthetic or authorized.
- Repository setup now also excludes local `samples/`, `backups/`, runtime data, debug captures, databases, and populated environment files from Git. The automated legacy-migration check uses a deliberately synthetic fixture under `tests/fixtures/`.
- The compatibility extraction CLI is organized under `scripts/`, while all authoritative plans, milestone reports, runbooks, and visual documentation assets live under `docs/`.

## 3. Proposed architecture and technology choices

### 3.1 Architecture

Use a single deployable asynchronous Python service with clear internal boundaries. This avoids unnecessary infrastructure while retaining upgrade paths.

```text
Telegram updates
    -> Telegram handlers and callback sessions
       -> authorization + group scope
       -> intake/application services
          -> SQLAlchemy unit of work -> SQLite
          -> managed receipt storage
          -> extraction queue/worker
             -> Gemini adapter OR OpenRouter adapter
          -> reconciliation service
          -> deterministic report service
    <- Persian command replies / inline keyboards / private detail views
```

The extraction worker is an in-process asynchronous database-backed worker. Its durable job state lives in the database, so interrupted download/extraction jobs can be recovered on startup. Network AI work does not hold a database write transaction and runs outside the Telegram update handler.

### 3.2 Package boundaries

Planned structure (final names may change only if documented before implementation):

```text
src/receipt_bot/
  config.py                 validated environment configuration
  domain/                   enums, money, lifecycle rules, reconciliation
  persistence/              SQLAlchemy models, repositories, unit of work
  extraction/               schemas, validation, preprocessing, providers
    providers/gemini.py
    providers/openrouter.py
  telegram/                 commands, callbacks, authorization, Persian views
  reporting/                deterministic queries, projections, renderers
  cleanup/                  eligibility, preview, deletion service
  logging.py                redaction and correlation context
migrations/                 Alembic migrations
tests/                      unit, integration, Telegram-handler contract tests
```

Dependencies should remain small:

- A small internal async Telegram Bot API client on the existing `httpx` dependency. Step 03 selected this over `python-telegram-bot` to keep the MVP dependency surface small while retaining protocol-boundary tests, long polling, inline callbacks, membership checks, user selection, and file download.
- SQLAlchemy 2.x plus Alembic for storage and migrations.
- SQLite in WAL mode for the single-instance MVP. PostgreSQL is an upgrade path, not an MVP service.
- Pydantic 2 and `pydantic-settings` for validated schemas/config.
- Existing `google-genai` and Pillow for direct Gemini and image work.
- `httpx` for Telegram, OpenRouter, and provider transport boundaries, with SOCKS support where configured.
- `pytest` and focused test helpers. Async Telegram behavior is driven deterministically with `asyncio.run`; Telegram APIs and AI calls are mocked at protocol boundaries.
- Python `zoneinfo` for Tehran timestamps. Add a maintained Jalali library only if Step 01 confirms it is needed for reliable user date entry; do not implement calendar arithmetic by guesswork.

### 3.3 Configuration and secrets

Configuration is environment-driven and validated at startup. Expected secret names will include `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and later `OPENROUTER_API_KEY`; provider/model IDs, DB URL, data directory, log level, retention days, and feature switches are non-secret settings. Secrets must never be logged or stored in the database. `.env.example` will contain placeholders only, and `.env`, databases, managed images, raw debug artifacts, and logs will be ignored by version control.

Production logs contain opaque record IDs, Telegram chat/message IDs only where operationally necessary, provider status/error class, latency, and correlation IDs. They must not contain receipt bytes, tokens, raw extracted data, full account identifiers, or full message text. Optional sensitive diagnostics default off and must reside under the managed data root with the same retention discipline as receipt images.

### 3.4 Persistence and concurrency

- SQLite foreign keys enabled, WAL mode, busy timeout, and short transactions.
- All money stored as integers; never binary floating point.
- UTC instants stored for ordering/audit, with `Asia/Tehran` used for report-day boundaries and presentation.
- Database writes use service-level transactions. Uniqueness constraints enforce Telegram and job idempotency.
- Financial record deletion is not part of normal operation. Corrections append revisions/audit events and preserve the original extraction.
- Receipt files live under one configured, resolved storage root. Database paths are relative managed keys, not arbitrary absolute paths.
- Regular database backup plus restore rehearsal is required by Step 07. SQLite database and live WAL files must be backed up using a safe SQLite backup method, not blind copying during writes.

## 4. Database model

Exact column names will be established by migrations, but the following entities and invariants are required.

### 4.1 Tenant, identity, and authorization

**telegram_groups**

- Internal ID, Telegram chat ID (unique), title, active flag, Tehran time zone, retention days (default 183), settings JSON/version, created/updated timestamps.

**telegram_users**

- Internal ID, Telegram user ID (unique), latest display name/username, created/updated timestamps. The numeric ID remains the stable database identity; the operator-requested bootstrap allowlist uses normalized usernames with the documented mutability risk.

**group_memberships**

- Group/user keys, role (`member`, `accountant`), active flag, grant/revoke actor and timestamps.
- Telegram group owner/administrator status is checked server-side for admin actions and refreshed from Telegram. Configured accountant status is group-scoped.

### 4.2 Source traceability

**telegram_messages**

- Group, Telegram chat/message/thread IDs, sender, reply-to message ID, update ID, timestamp, edit timestamp, type, minimally required text/caption, and ingestion state.
- Unique `(group_id, telegram_message_id)` and, where available, unique processed update ID.
- Every request, promise, receipt, or manual record references a source message or an audit event identifying its creator.

**counterparties**

- Group-scoped person/business identity, optional linked Telegram user, display name, normalized aliases, active flag.
- Names are never globally merged across groups. Ambiguous aliases require selection.

### 4.3 Requests, receipts, payments, and promises

**payment_requests**

- Group, counterparty, source message, requested amount/value/unit, canonical rial amount when unit is known, requested/ due timestamps, state (`draft`, `open`, `partially_documented`, `documented`, `closed`, `cancelled`, `needs_review`), created-by and revision number.

**receipt_images**

- Image/file artifact metadata only: group, source message, Telegram `file_id` and stable `file_unique_id`, original filename/MIME/size, SHA-256, managed relative storage key, storage state (`present`, `deleted`, `missing`, `delete_failed`), cleanup timestamps, duplicate disposition, created timestamp.
- This table is deliberately not the generic definition of a receipt. Text receipts never create placeholder image rows.

**payment_evidence**

- Generic group-scoped financial evidence referencing its original Telegram message, evidence kind (`image` or `telegram_text`), optional image artifact, normalized content hash, classification state, duplicate candidate/canonical evidence, and timestamp.
- Unique source-message ingestion and group-scoped hashes prevent Telegram replay and exact copied text/image evidence from becoming multiple countable payments. Cross-group matches remain independent and undisclosed.

**text_receipt_jobs / receipt_jobs**

- Separate durable acquisition queues reflect source mechanics: text evidence goes directly to extraction; image evidence first downloads and validates its managed file. Both converge on `payment_evidence` before an extraction attempt is recorded.

**extraction_attempts**

- Payment evidence, optional image artifact for backward-compatible image traceability, attempt number, provider/model/schema/prompt versions, status, immutable raw structured response, normalized candidate data, validation issues, field evidence/confidence/uncertainty, start/end timestamps, and redacted error class.
- Original extractions are append-only. Raw model output is protected application data, not routine log content.

**payments**

- Group, receipt and/or manual source, counterparty candidate, source numeric value/unit, canonical rial amount when reliable, printed transaction date/reference, evidence status (`submitted`, `needs_review`, `rejected`, `duplicate`, `confirmed`), settlement assertion (`not_checked`, `externally_confirmed` only through explicit accountant action), current revision, timestamps.
- `submitted` means documented by a receipt; it does not mean bank settlement.

**request_payment_allocations**

- Group, request, payment, allocated rial amount, state, creator and timestamps.
- Enables partial and split payments without counting the full receipt once per request. Active allocation totals may not exceed the reliable payment amount or request amount without an explicit discrepancy/review outcome.

**payment_promises**

- Group, counterparty, source message, promised amount/unit/canonical amount, expected date, state, and optional request link.
- Promises are shown separately and never included in submitted or confirmed payment totals.

### 4.4 Review, correction, and reporting

**reviews**

- Group, target type/ID, reviewer, decision (`confirmed`, `corrected`, `rejected`, `marked_duplicate`, `reopened`), comment/reason, before/after revision references, timestamp.

**record_revisions**

- Append-only normalized snapshots/patches for correctable request, receipt-extraction, payment, allocation, promise, and counterparty fields; actor and reason required.

**audit_events**

- Group, actor, action, target, source/correlation identifiers, safe structured metadata, timestamp. Audit rows are immutable through normal application paths.

**report_runs**

- Group, report kind, Tehran period start/end, as-of timestamp, requester, calculation version, filter parameters, included source record/revision IDs or a reproducible manifest hash, output hash/status.
- Reports are calculated from current authoritative records; this entity supplies reproducibility and traceability, not a second ledger.

**interaction_sessions**

- Opaque callback token, group, initiating user, workflow/state, target IDs, expiry and consumption timestamp. Callback payloads do not trust role, amount, or arbitrary paths supplied by clients.

### 4.5 Mandatory isolation and integrity invariants

- Every financial query is group-scoped in the repository/service API; cross-group foreign-key relationships are rejected.
- Telegram callback actions re-check the initiating user, current group, role, target group, expiry, and allowed state transition server-side.
- Original source/extraction/audit records are not overwritten during correction.
- A duplicate/rejected payment contributes zero to financial totals.
- One payment's amount is counted once, through active allocations or once as unallocated submitted evidence—never once per possible match.
- Unknown currency units and unreliable amounts never enter comparable rial/toman totals.
- Cleanup changes only receipt storage state/path metadata after attempting an eligible file operation; it never cascades to financial entities.

## 5. Transaction lifecycle and workflows

### 5.1 Intake and classification

1. Telegram update is idempotently recorded with group and sender references.
2. The service classifies candidate content as a payment request, payment promise, receipt/document, correction/review action, or non-financial content. Non-financial content is minimized according to the retention setting.
3. Image receipt bytes are downloaded into the managed group partition using a generated storage key; MIME, size, decode, pixel, and path checks run before extraction. Text receipts retain their exact Telegram source and enter the provider-independent text path without creating an image artifact.
4. Exact Telegram identity and group-scoped normalized content hashes do not create another countable payment. Similar reference/date/amount matches become non-counting duplicate candidates until reviewed.
5. AI extraction creates an immutable attempt. Deterministic normalization/validation creates a candidate payment or a review issue.
6. Matching suggestions use group-scoped, deterministic facts: explicit reply/reference, selected counterparty, reliable amount, source time window, and transaction reference. AI may help identify candidates but cannot commit an association.
7. A unique, strong explicit link may be auto-proposed; ambiguity always requires an accountant selection. No association is invented.

### 5.2 Review and correction

1. `/pending` shows group-scoped review queues with non-sensitive summaries.
2. Selection opens an authorized interaction, preferably in private chat for account numbers, full descriptions, images, and correction details. The group receives only a safe status/update.
3. Reviewer sees the original source reference, extracted fields, uncertainty/issues, duplicate candidates, and proposed request allocations.
4. Reviewer may confirm, correct, reject, mark duplicate, change/select counterparties, or link/split/unlink allocations. A reason is required for material changes.
5. The service validates the transition and amounts, writes revision/review/audit rows in one transaction, and then regenerates a concise result.

No confirmation means independent bank settlement unless an accountant explicitly records external confirmation. Receipt confirmation ordinarily means “accounting reviewed as documented evidence.” The UI must state this distinction.

### 5.3 Currency and number rules

- Preserve the printed numeric value and printed/detected unit separately.
- Normalize Persian (`۰..۹`) and Arabic-Indic (`٠..٩`) digits deterministically before numeric parsing.
- Accept separators only through explicit locale-aware normalization; reject malformed or negative receipt amounts.
- Unit is one of `rial`, `toman`, or `unknown`. Never silently assume an unknown unit.
- Convert known toman to canonical rial with exact multiplication by 10. Retain the source value/unit.
- Reports requested in toman divide exact rial amounts using decimal/integer logic. If rial is not divisible by 10, display the exact fractional toman and rial equivalent rather than round silently.
- Persian output uses Persian digits and `٬` grouping, with `تومان` stated in headings and values.

### 5.4 Reconciliation rules

For each payment request, calculate deterministic projections:

- `requested_rial`: reliable canonical requested amount.
- `submitted_rial`: unique, non-rejected/non-duplicate receipt payment allocations, whether reviewed or not.
- `confirmed_rial`: allocations whose payment and association are accounting-confirmed under the defined workflow.
- `unverified_rial = submitted_rial - confirmed_rial` where comparable.
- `documented_difference_rial = requested_rial - submitted_rial` only when request and all included values have known units and valid allocations.
- `confirmed_difference_rial = requested_rial - confirmed_rial`, labeled as a confirmation gap, not debt.

The report must not call either difference the person's overall debt. A per-request difference may be negative (documented overage) and must be shown as such. Promises appear in a separate informational line and never reduce differences. Unallocated receipts appear in unresolved totals, not against every matching request.

If the request amount/unit, receipt amount/unit, counterparty, duplicate status, or association is ambiguous, the report must show “اطلاعات ناکافی / نیازمند بررسی” and exclude the uncertain value from any total that would imply false precision. It may also show a separately labeled uncertain amount range/list when exact components are traceable.

### 5.5 Report traceability

Every line and aggregate is backed by a report projection containing request IDs, payment/allocation IDs, active revision IDs, source Telegram chat/message IDs, and exclusion/warning reasons. Aggregate totals are sums of the displayed eligible line components, not independent AI outputs. Report rendering never asks an AI model to calculate or rewrite money.

The standard footer in Persian must state that receipts are not independent proof of bank settlement and that differences are not final debt until accounting review.

## 6. Telegram command and interaction design

Commands default to the current group. In private chat, the user selects only among groups for which current server-side authorization is verified. Commands with sensitive drill-down use short-lived opaque inline callback sessions and may prompt the user to continue privately.

| Command | Access | MVP behavior |
|---|---|---|
| `/start` | Public support in private; allowlisted current admin for group activation | Shows Persian introduction/support. Group registration and activation occur only after both the environment allowlist and live Telegram administrator checks pass. |
| `/help` | Any user; role-aware | Shows only useful workflows, but hiding an item is not authorization. Includes brief Persian examples. |
| `/reports` | Group admin, accountant | Today's Tehran-date accounting report by default; inline previous/next day, custom range, refresh, details, and private export/navigation. Primary feature. |
| `/receipts` | Group admin, accountant | Date selector, paginated receipts, extraction/storage/review status, safe group summary, private sensitive details. |
| `/pending` | Group admin, accountant | Paginated unresolved extraction, duplicate, unit, party, matching, and verification queues with severity/reason filters. |
| `/balance` | Group admin, accountant | Select a counterparty; show per-request requested/submitted/confirmed/uncertain values. Explicitly refuses an overall balance when evidence is insufficient. |
| `/transactions` | Group admin, accountant | Browse requests, promises, payments, allocations, and source links by date/person/status without merging their meanings. |
| `/verify` | Group admin, accountant | Select a pending record or accept a bot-issued opaque reference; confirm/correct/reject/duplicate/link/split workflow with final confirmation. |
| `/summary` | Group admin, accountant | Period totals for requested, submitted, confirmed, promised, unresolved, and excluded/uncertain items; links into `/reports`. |
| `/cleanup` | Group admin only | Preview eligible files and bytes, require explicit confirmation, run safe deletion, show failures; financial data remains. |
| `/settings` | Group admin only | Group activation, reviewer roles, retention (default 183 days), AI provider/model choices, notification/privacy options. Material changes require confirmation/audit. |

No extra top-level command is needed for correction: it belongs under `/verify`. Cancel/back/home inline buttons are required for multi-step workflows. Callback queries must be acknowledged quickly, stale screens must fail safely, pagination must use stable IDs rather than user-provided offsets alone, and irreversible file cleanup must use a fresh preview token plus a second confirmation.

Telegram source references use deep links when Telegram permits them and otherwise show chat/message identifiers. A bot cannot recover group history from before it was present; reporting begins with captured or deliberately imported records. Group collection also depends on bot permissions/privacy-mode configuration, which Step 07 must document and validate.

## 7. Reporting output contract

For a selected Tehran date/range, `/reports` must include:

1. Period and explicit unit (`تومان`).
2. One section per request/person: requested amount, uniquely allocated receipt amount, confirmed portion, unverified portion, difference where reliable, accounting status, and unresolved warnings.
3. Unallocated receipts and promises in separate sections.
4. Group totals derived from eligible displayed components: requested, submitted receipt values, confirmed, unverified, and comparable differences.
5. Counts/amounts excluded because of unknown units, duplicates, ambiguity, rejection, or insufficient information.
6. Standard non-settlement/non-debt warning.
7. Traceability action that resolves to exact source records for authorized users.

Date filters use the Telegram message/request occurrence time for requests and receipt submission time for receipt browsing; the report exposes its chosen basis. Printed transaction time is preserved and may be an optional secondary filter only after validation. Historical edits do not silently rewrite report reproducibility: the current report shows an “as of” time, while `report_runs` retain the calculation version/manifest.

## 8. Sequential seven-day implementation milestones

### Step 01 / Day 1 — Application foundation, domain types, and persistence

**Scope**

- Establish package layout, reproducible dependency/config setup, `.gitignore`, safe `.env.example`, test configuration, and structured/redacted logging.
- Define domain enums/value objects, especially money/unit/status types and Tehran time handling.
- Implement SQLAlchemy/Alembic schema for the required entities and group-scoped repository/unit-of-work boundaries.
- Configure SQLite integrity/WAL behavior and managed data-root validation.
- Seed no real financial data and do not implement Telegram or AI behavior yet.

**Automated tests / acceptance**

- Clean database migrates from zero to head and can be recreated.
- Money conversion/format primitives use exact arithmetic, normalize both Persian digit sets, and preserve unknown units.
- Repository tests prove same Telegram message cannot be ingested twice and cross-group associations are rejected.
- Correction/audit structures are append-only through repository APIs.
- Configuration fails clearly for missing/invalid required settings without printing secrets.
- Storage-root resolver rejects traversal/out-of-root keys.

**Deliverable**: `docs/STEP_01_REPORT.md`; stop after tests pass and plan status is updated.

### Step 02 / Day 2 — Provider-independent receipt extraction

**Scope**

- Refactor reusable preprocessing/checksum behavior from `extract_receipt.py` behind application modules while retaining CLI compatibility where practical.
- Define provider-neutral extraction input/output, field evidence/uncertainty, validation issues, and typed error taxonomy.
- Add Gemini adapter and OpenRouter adapter selected by configuration. Start with a configured Gemini vision-capable model; model IDs remain configuration, not accounting logic.
- Validate image type/size/pixels, numeral/unit/date/account/reference fields, and immutable extraction attempts.
- Make sensitive diagnostic capture opt-in and managed. Never log credentials or raw financial content.

**Automated tests / acceptance**

- Golden/mock responses cover Persian and Arabic-Indic numerals, printed and handwritten uncertainty, rial, toman, unknown unit, missing fields, invalid IBAN, malformed JSON, refusal, timeout, rate limit, and provider fallback/error mapping.
- Gemini and OpenRouter adapters satisfy the same contract without live credentials.
- Preprocessing tests cover EXIF, transparency, resizing, original-byte fallback, corrupt/oversized/unsupported files, deterministic hash, and no path escape.
- Existing sample JSON can be migrated into the neutral schema without claiming OCR accuracy.
- No test calls a paid/live API. Live Gemini smoke test is separately documented and opt-in; OpenRouter live test remains pending until its key exists.

**Deliverable**: `docs/STEP_02_REPORT.md`; stop.

### Step 03 / Day 3 — Telegram intake, isolation, authorization, and basic commands

**Scope**

- Add polling-based async Telegram service for the MVP, update idempotency, group registration, source references, Telegram-text receipt intake, photo/document download, and durable extraction jobs.
- Implement `/start`, `/help`, and `/settings` foundations in Persian.
- Enforce current Telegram admin status and group-scoped accountant roles in service code for commands and callbacks.
- Add opaque expiring interaction sessions and private-detail handoff.
- Implement exact receipt duplicate prevention and conservative candidate-duplicate status.

**Automated tests / acceptance**

- Replayed updates/messages/photos produce one source/evidence/job; copied same-group text receipts are marked exact duplicates and identical cross-group text remains isolated.
- Two groups using the same users/files remain isolated; no response or query leaks cross-group data.
- Unauthorized users cannot call management/reviewer services even by forging callback data; revoked roles stop working.
- Only admins can change settings/reviewers; settings changes create audit events.
- Receipt download failures/restarts yield recoverable states without duplicate accounting records.
- Persian basic command text and callback expiry/back/cancel paths are snapshot/contract tested.

**Deliverable**: `docs/STEP_03_REPORT.md`; stop.

### Step 04 / Day 4 — Reconciliation and deterministic reporting engine

**Scope**

- Implement request, promise, payment, allocation, review-state, duplicate, and correction lifecycle services.
- Add deterministic candidate matching and explicit ambiguity results.
- Implement report projections, group aggregates, exclusions, trace manifests, and Persian money/report renderer.
- Prioritize the `/reports` data contract even though Telegram presentation is Step 05.

**Automated tests / acceptance**

- Exact, partial, split, overpayment, multiple receipts, unallocated receipt, cancelled request, duplicate, rejected, promise-only, unknown unit, ambiguous person, corrected amount, and date-boundary cases.
- A payment linked to multiple candidates is never double-counted; allocation sums and invariants hold transactionally.
- Submitted and confirmed totals remain distinct; promises contribute to neither.
- Differences are per request, never labeled overall debt; unreliable cases emit warnings and are excluded from misleading totals.
- Aggregate values exactly equal eligible detail lines and every value resolves to source/revision IDs.
- Property/invariant tests use integer arithmetic and demonstrate no float path.

**Deliverable**: `docs/STEP_04_REPORT.md`; stop.

### Step 05 / Day 5 — Telegram reports, review, correction, and browsing workflows

**Scope**

- Implement `/reports` first, then `/summary`, `/pending`, `/verify`, `/receipts`, `/transactions`, and `/balance` using the tested services.
- Add Tehran day/range selection, person/receipt selection, pagination, safe group summaries, private sensitive views, source navigation, and stale-session handling.
- Implement correction/link/split/duplicate/reject/confirm flows with final confirmation and reason capture.

**Automated tests / acceptance**

- Command defaults, Persian formatting, sample report contract, date navigation/ranges, pagination, and empty/error states pass.
- Each command has admin/accountant/unauthorized and forged-callback authorization tests.
- Corrections preserve original extraction and add revision/review/audit records.
- Sensitive receipt/account details are absent from public group output and available only through authorized private workflow.
- `/balance` distinguishes confirmed/uncertain values and warns/refuses precision when insufficient.
- Callback retry does not apply a material action twice.

**Deliverable**: `docs/STEP_05_REPORT.md`; stop.

### Step 06 / Day 6 — Safe receipt image cleanup

**Scope**

- Implement admin-only `/cleanup` preview and confirm flow with configurable 183-day default.
- Calculate eligible file count/bytes without following symlinks or accepting arbitrary paths.
- Delete only eligible managed image files; update storage state safely and preserve all financial/extraction/audit records.
- Support partial failure and rerun without corruption.

**Automated tests / acceptance**

- Unauthorized/accountant-only users cannot preview or execute cleanup.
- Fresh/ineligible files, unknown paths, traversal, symlinks, directories, and files outside the resolved storage root are never deleted.
- Confirmation must match an unexpired preview and re-evaluates eligibility at execution.
- Success, already-missing, permission/error, and partial-failure cases keep database consistency.
- Row counts/content for requests, extraction attempts, payments, allocations, reviews, reports, revisions, and audits are unchanged after cleanup, except storage/audit metadata.
- UI clearly shows count, estimated bytes, retention cutoff, permanence, and that accounting data remains.

**Deliverable**: `docs/STEP_06_REPORT.md`; stop.

### Step 07 / Day 7 — Deployment, reliability hardening, and MVP acceptance

**Scope**

- Add production container/service configuration for one persistent bot instance, health/startup checks, graceful shutdown, extraction-job recovery, log rotation/redaction, backup/restore instructions, and operator runbook.
- Document Telegram BotFather privacy/permissions, group onboarding, secrets, provider selection, costs, retention, incident recovery, and data privacy.
- Run the full automated suite and an isolated staging Telegram acceptance checklist.
- Run live provider smoke tests only when credentials are explicitly supplied; record unavailable checks as pending, never passed.

**Automated/manual acceptance**

- Fresh deployment migrates and starts with persistent DB/image volumes; restart recovers queued work without duplicate records.
- Full tests pass in a clean environment, including authorization, extraction mocks, reconciliation/report accuracy, isolation, and cleanup safety.
- Backup restores to a separate location and produces matching integrity checks/report fixture.
- Secret/redaction scan finds no committed/live credentials or financial content in routine logs.
- Staging Telegram flow validates onboarding, receipt upload, request/receipt review, report, correction, duplicate replay, private details, and cleanup preview/confirmation.
- Gemini live smoke test status is explicitly reported. OpenRouter live smoke test is pending until `OPENROUTER_API_KEY` is provided.

**Deliverable**: `docs/STEP_07_REPORT.md`, operating documentation, and final acceptance matrix; stop.

## 9. Testing strategy

- Unit tests: money, Persian digits, validators, state transitions, matching, calculations, formatters, authorization predicates, paths.
- Database integration tests: migrations, constraints, transactions, idempotency, revisions/audits, tenant isolation, cleanup invariants.
- Adapter contract tests: both AI providers with captured synthetic/mocked responses; Telegram client protocol mocked.
- Handler tests: commands/callbacks against service fakes plus selected end-to-end tests against a temporary SQLite database.
- Golden report tests: human-readable Persian output plus machine projection checks; expected totals are manually declared, never generated by the code under test.
- Property/invariant tests where valuable: allocations cannot create/destroy money, duplicates count zero, aggregates equal details, group IDs never mix.
- Live tests: tagged/opt-in, never part of deterministic CI, never claimed passed without credentials and an actual call. Use synthetic/non-sensitive images where possible.

Each step report must list exact commands, pass/fail/skip counts, skipped reasons, environment assumptions, and unverified behaviors. A milestone cannot be marked complete if a mandatory deterministic test is failing. A credential-gated live check may remain pending when the adapter's credential-independent contract tests pass and the limitation is explicit.

## 10. Credential dependencies, risks, and unresolved decisions

### Credentials/dependencies

- Telegram bot token is required from Step 03 for staging/live operation; automated handler tests do not require it.
- `RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES` is mandatory and fail-closed. It accepts unique ASCII Telegram usernames, optionally prefixed with `@`, and compares them case-insensitively.
- Direct Gemini key is required only for opt-in live extraction; deterministic implementation/tests mock the provider.
- OpenRouter key is unavailable. Implement the adapter and mocked contract tests in Step 02; do not claim a live check.
- Telegram bot must be added to each group with sufficient permissions and privacy mode configured to receive the intended financial messages. Exact least-privilege settings must be validated in staging.

### Principal risks and mitigations

| Risk | Impact | Planned mitigation |
|---|---|---|
| OCR/handwriting error | Incorrect amount/party/reference | Field uncertainty, deterministic validators, review queue, immutable original, no auto-confirmation. |
| Rial/toman ambiguity | 10x financial error | Source value/unit plus `unknown`; no silent conversion; reviewer correction. |
| Duplicate photos/messages | Double counting | Telegram idempotency, stable file ID, SHA-256, duplicate state, allocation invariants. |
| False request association | Misstated person/request | Explicit reply/selection first, conservative candidates, review ambiguity, traceable unlink/correction. |
| Receipt treated as settlement | False confirmation | Separate submitted/reviewed/external-settlement states and mandatory report disclaimer. |
| Group/privacy leak | Severe confidentiality issue | Group-scoped repositories, server authorization on every action, private drill-down, isolation tests. |
| Unauthorized provider consumption | Credential cost/data exposure | Required username bootstrap allowlist, live admin check at activation, persisted approved-group anchor, gates on commands/callbacks/intake/recovery/worker materialization, fail-closed configuration. |
| Username change/reassignment | Authorization identity may become stale | Persist numeric ID for audit, require reactivation when configured username changes, document weaker guarantee and recommend immediate removal/restart on compromise. |
| Debug/storage leak | Sensitive financial exposure | Debug off, redacted logs, managed storage, retention cleanup, secret/data scan. |
| SQLite/concurrent workers | Locks or duplicate jobs | Single instance, WAL, short transactions, DB idempotency; defer horizontal scale/PostgreSQL. |
| Provider/model churn | Extraction outage | Configured model IDs, provider interface, typed retry/fallback, pending queue, health/runbook. |
| Telegram history limitation | Incomplete historical reports | Explicit reporting coverage/warnings; no claim about messages the bot never received. |
| Seven-day/budget constraint | Incomplete polish | Strict sequential MVP, report correctness before convenience, no web UI/external integrations. |

### Decisions to resolve before or during the named step

1. **Data retention/privacy (resolved for MVP in Step 03):** retain original financial group-message text because later request/promise classification and source traceability require it. Completed raw update envelopes are cleared; failed envelopes remain only for recovery. Original images/details are restricted to current group admins and active group accountants through private workflows. A shorter text-retention policy remains a post-MVP/privacy-policy option.
2. **Accounting meaning of “confirmed” (implemented in Step 04):** `confirmed` means accountant-reviewed documentary evidence plus a confirmed allocation. Independent bank settlement remains a separate `externally_confirmed` assertion and is never inferred from a receipt.
3. **Auto-link threshold (implemented in Step 04):** only a unique explicit request/reply relationship is eligible for a unique deterministic suggestion. Amount/counterparty compatibility narrows candidates but never resolves multiple candidates; no suggestion writes an allocation. Any broader auto-linking requires a future documented decision and acceptance tests.
4. **Report date basis (implemented in Step 05):** default reports use request timestamps and receipt submission timestamps with Tehran day boundaries. Printed transaction time is preserved and shown only as secondary receipt detail. Inline buttons provide today, previous/next period, and rolling 7/30-day selection; the optional typed date is explicitly Gregorian `YYYY-MM-DD` to avoid silently interpreting Jalali input as Gregorian.
5. **Deployment target (resolved in Step 07):** one Linux Docker Compose host, one bot replica, a persistent named data volume, and a separately mounted backup directory with encrypted off-host replication by the operator.
6. **Legacy samples (deployment-safe, ownership decision pending):** `samples/` and `debug/` are excluded from container builds and source/runtime security scans. The owner must still confirm that they are synthetic/authorized before any source-control publication; otherwise remove them through an explicitly approved data-handling action.
7. **Retention days:** default is 183 days as the practical six-month value. Confirm whether the client requires calendar-month cutoff instead.
8. **Telegram bootstrap allowlist (revised at owner request):** a group remains authorized only while active and its recorded activating username remains in the environment allowlist. Removing that username revokes processing immediately after restart; a different allowlisted current administrator can reactivate the group without deleting its records. The stable numeric actor ID remains recorded for audit only.

None of these decisions authorizes weaker isolation, invented values, double counting, or destructive deletion of accounting data.

## 11. Final MVP acceptance criteria

The MVP is accepted only when all of the following are evidenced in `STEP_07_REPORT.md`:

- Authorized Telegram groups can capture new financial requests, promises, receipt text/images, senders, and source message references without cross-group leakage.
- Printed and handwritten Persian receipt inputs are processed through Gemini with an OpenRouter-compatible adapter available; unreadable/uncertain/unit-ambiguous values are visibly pending human review.
- Telegram/update/file/hash duplicate cases cannot double-count a payment.
- Accountants can confirm, correct, reject, link/split, or mark duplicates through authorized inline workflows, while originals and audit history remain intact.
- `/reports` produces correct Tehran-date/range, per-request and group totals for requested, submitted, confirmed, unverified, differences, and unresolved items using deterministic integer/decimal logic and traceable source records.
- `/summary`, `/pending`, `/verify`, `/receipts`, `/transactions`, and `/balance` meet their role, privacy, ambiguity, and formatting contracts.
- Reports never imply that a receipt proves settlement, a promise is payment, or a request-level difference is a person's total debt.
- `/cleanup` previews and deletes only eligible managed images after admin confirmation and demonstrably preserves all accounting/extraction/audit records.
- Database migrations, restart recovery, backup/restore, configuration, redacted logging, and operator documentation work in a clean deployment.
- All mandatory automated tests pass; every skipped live check and remaining limitation is explicit. No untested behavior is labeled verified.

Out of scope remains WhatsApp, guaranteed handwriting recognition, custom OCR training, independent bank settlement verification, external accounting integrations, and a web dashboard.
