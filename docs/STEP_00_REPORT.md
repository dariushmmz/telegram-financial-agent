# Step 00 Report — Repository Assessment and Planning

## Status

**Complete — planning only.** No application feature, dependency, database, Telegram handler, or extraction behavior was implemented or changed in this step.

- Date: 2026-10-19
- Milestone: Step 00
- Deliverables: `docs/PROJECT_PLAN.md`, `docs/STEP_00_REPORT.md`
- Next milestone: Step 01 / Day 1 — Application foundation, domain types, and persistence
- Gate: work must stop after this report and await explicit authorization.

## Repository assessment

The repository contains a standalone `extract_receipt.py` prototype, five sample JPEG files, one sample JSON result, a debug folder with sent-image/log artifacts, and a `.env`. It is not currently a Git working tree. No package/dependency manifest, tests, migrations, database, Telegram bot implementation, deployment files, `.gitignore`, or project README were found.

The local environment used for this assessment reports Python 3.12.6, `google-genai` 1.54.0, Pydantic 2.12.5, and Pillow 10.2.0. These are observations, not yet project-pinned requirements.

The `.env` file was inspected by variable name only; secret values were not printed. It declares `API_KEY`, `BOT_TOKEN`, `BOT_ID`, and `GROUP_URL`. The extraction script reads `GEMINI_API_KEY` and does not load `.env`, so local configuration is currently inconsistent. The absence of `.gitignore` makes any future source-control initialization a credential/data-exposure risk until Step 01 addresses it.

Post-assessment update (2026-10-20): the Google AI Studio key variable was renamed in place from `API_KEY` to `GEMINI_API_KEY` without displaying or changing its value. The original paragraph above is retained as the historical Step 00 finding.

## Findings from `extract_receipt.py`

### Existing capabilities worth preserving

- Direct Gemini integration through the official `google-genai` client.
- Pydantic response schema and JSON response mode at temperature zero.
- Prompt rules for Persian text, Persian/Arabic-Indic numeral normalization, printed Jalali date retention, masked accounts, and nulls instead of guessing.
- Conservative image processing: EXIF transpose, safe alpha flattening, no upscaling, high-resolution downscale threshold, optional grayscale/enhancement, damage fallback, and lossless PNG for modified images.
- SHA-256 calculation and detailed preprocessing provenance.
- Limited temporary-error retry/model fallback and a model-list command.
- Deterministic Iranian IBAN checksum validation.

### Integration and extraction limitations

- Provider logic, prompt, schema, filesystem diagnostics, and CLI termination are tightly coupled.
- There is no OpenRouter adapter or provider-neutral contract.
- The model fallback list is hard-coded. A historical debug run names `gemini-3.8-flash`, but future availability is not guaranteed.
- The schema forces the amount to rial and cannot safely represent toman or unknown unit.
- There is no field-level uncertainty/evidence, extraction version, or review state.
- The warning Boolean defaults false, which cannot distinguish “absent” from “unreadable.”
- Only the destination IBAN checksum is validated; amount/date/account/reference consistency is otherwise trusted.
- `sys.exit` is used from reusable functions, and exceptions are not mapped to durable/recoverable jobs.
- The CLI names JPG/PNG/WebP, but implementation has no explicit content allowlist or upload size/pixel/multi-frame security policy.
- Existing evidence covers a bank screenshot sample only. Printed paper photos and handwriting were not tested in this step and must not be reported as verified.

### Privacy and operational limitations

- Every call copies the model-bound image and writes raw model text plus extracted financial data to local debug files by default.
- Debug artifacts have no authorization, encryption, or cleanup policy, and second-resolution names may collide.
- Naive local timestamps are used rather than UTC instants plus `Asia/Tehran` presentation.
- Receipt hashes are computed but not persisted for duplicate control.
- No logs/records correlate data to a Telegram group/message while enforcing tenant isolation.
- No live Gemini call was made during Step 00; no OpenRouter credential exists.

## Planning decisions

The authoritative roadmap is in `docs/PROJECT_PLAN.md`. The principal decisions are:

- One asynchronous Python service and one persistent database; no web dashboard or unnecessary service split.
- `python-telegram-bot`, SQLAlchemy/Alembic, SQLite WAL for the single-instance MVP, Pydantic, existing Gemini/Pillow libraries, and mocked protocol-boundary tests.
- Canonical integer rial arithmetic with original value/unit retained; unknown units never silently enter totals. Toman presentation remains exact.
- Immutable extraction attempts and append-only revisions/reviews/audits; corrections do not destroy originals.
- Separate payment requests, receipt-backed submitted payments, accounting-confirmed allocations, payment promises, and external settlement assertions.
- Group-scoped repositories and server-side role checks for every command/callback.
- Deterministic reconciliation/reporting only. AI may extract or suggest; it never calculates totals or commits ambiguous associations.
- `/reports` is the primary workflow. Detail lines, aggregates, exclusions, warnings, and source manifests share the same deterministic projection.
- Sensitive details move to authorized private interactions; group responses remain concise and safe.
- Cleanup deletes only eligible managed image files after an admin preview/confirmation and never deletes financial records.
- Seven sequential implementation milestones follow this planning step; exactly one may be completed per authorized execution.

## Validation performed

The following read-only or documentation checks were performed:

1. Enumerated all visible repository files, including hidden top-level files.
2. Read all 388 lines of `extract_receipt.py` and traced its preprocessing, Gemini request, retry, validation, diagnostic, CLI, and output paths.
3. Read the sample extraction JSON and inspected non-secret debug metadata (model, image metadata, processing steps, warnings, and result field names).
4. Inspected `.env` keys without exposing their values.
5. Ran `python --version` and inspected installed versions for the three direct third-party imports.
6. Ran `python extract_receipt.py --help`; it completed successfully, demonstrating that the current imports and argument-parser path load in this environment.
7. Checked for common dependency, test, documentation, Git, ignore, and deployment files; none were present before this step.
8. Validated that this directory is not currently a Git repository.
9. Reviewed both created Markdown documents for required sections, milestone gates, test criteria, progress status, and absence of copied secret values.

No paid API call, Telegram call, OCR accuracy test, database test, or application feature test was performed. Accordingly, none is claimed as verified.

## Risks and dependencies

- Current `.env` naming/loading mismatch will prevent the script from finding a key unless the process environment is populated separately.
- Existing sample/debug artifacts appear to contain financial personal information and require an owner decision before source control or deployment.
- Telegram bot permissions/privacy mode and access to relevant group messages must be configured and staged.
- Gemini model availability and cost are external dependencies. Provider/model selection must be configurable.
- OpenRouter credentials are unavailable; only credential-independent adapter tests can be completed initially.
- Handwriting accuracy is inherently variable and will require uncertainty/review behavior rather than a guarantee.
- The meaning of accounting “confirmed,” default report date basis, text retention policy, deployment target, and calendar-vs-183-day retention cutoff require the decisions listed in the plan.
- SQLite is appropriate only for the planned single-instance MVP; horizontal workers require a database/queue reassessment.

## Files changed

- Added `docs/PROJECT_PLAN.md`.
- Added `docs/STEP_00_REPORT.md`.

No existing file was modified.

## Next milestone

After explicit authorization, implement **Step 01 / Day 1 — Application foundation, domain types, and persistence** only. Its completion requires migrations from an empty database, exact money and Persian-digit tests, group-isolation/idempotency constraints, append-only correction/audit behavior, safe configuration, managed-path rejection tests, a plan progress update, and `docs/STEP_01_REPORT.md`.

Implementation must not proceed to Step 02 in the same execution.
