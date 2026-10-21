# Step 02 Report — Provider-independent Receipt Extraction

## Status

**Complete and deterministically tested.** This execution implemented Step 02 only. It did not implement Telegram update handling, authorization, financial matching/reconciliation, user-facing reports, review commands, or cleanup.

- Date: 2026-10-20
- Milestone: Step 02 / Day 2
- Final automated result: **73 passed, 0 failed, 0 skipped**
- New Step 02 tests: 38; retained Step 01 tests: 35
- Migration head: `2f4d9a10b7c1`
- Gemini live smoke test: **passed for API transport and structured extraction** (`gemini-3.8-flash` through the configured SOCKS proxy)
- OpenRouter live smoke test: **pending; API key unavailable**
- Next milestone: Step 03 / Day 3 — Telegram intake, isolation, authorization, and basic commands
- Gate: stop after this report and await explicit authorization.

## Changes implemented

### Provider-neutral extraction contract

Added `receipt_bot.extraction` with one common contract for direct Gemini and OpenRouter:

- `ProviderReceipt` retains values close to the printed source.
- `NormalizedReceipt` contains deterministic normalized values, canonical rial only when safe, validation issues, uncertain fields, and `requires_review`.
- `ProviderResult` identifies provider/model/request and retains the successful structured provider observation separately from normalization.
- `ExtractionOutcome` combines provider output, normalized data, original/sent image hashes, MIME type, and preprocessing provenance.
- `FieldEvidence` records field, observed text, confidence (`certain`, `likely`, `uncertain`, or `unreadable`), and an optional note.
- Document kind distinguishes bank screenshots, printed receipts, paper photos, handwritten forms, mixed documents, and unknown inputs.

The versioned Persian extraction prompt explicitly prohibits guessing, request/person association, silent unit inference, calendar conversion, and settlement claims. It asks for nulls and field evidence when values are missing or unreadable.

### Deterministic financial normalization and validation

- Persian and Arabic-Indic digits normalize through the Step 01 integer parser.
- The provider returns `amount_value` as source text and a separate `amount_unit` enum.
- Known toman converts to canonical rial by exact multiplication by 10.
- Known rial remains exact.
- Unknown/ambiguous unit preserves the numeric source value, sets canonical rial to null, and creates a blocking review issue. No 10x assumption occurs.
- Missing, zero, malformed, or database-overflow amounts create explicit error issues.
- Iranian IBAN is normalized and verified with mod-97.
- Source accounts retain masks while unexpected characters are flagged.
- Printed transaction date/time retains its calendar and is shape/range validated without conversion.
- Tracking codes normalize both Persian digit sets and are flagged if not digits-only.
- Critical fields marked likely, uncertain, or unreadable route to human review according to the confidence policy.
- The receipt warning is nullable, so unreadable is no longer conflated with false.

### Secure image validation and reusable preprocessing

The conservative behavior from the prototype was moved into `receipt_bot.extraction.image` and strengthened:

- accepts decoded JPEG, PNG, and WebP only;
- validates content rather than trusting filename/MIME;
- rejects empty, corrupt, mismatched, unsupported, oversized, and excessive-pixel inputs before provider use;
- defaults to 15 MiB, 40 million pixels, and a 4096-pixel provider-bound side;
- preserves validated original bytes when no processing is needed;
- applies EXIF orientation, safe white alpha flattening, selected color-mode conversion, and no-upscale resizing;
- keeps grayscale/enhancement opt-in;
- writes modified images as lossless PNG;
- falls back to original bytes if a processed result looks uniform/black;
- records deterministic SHA-256 hashes, dimensions, format/mode, options, steps, and warnings.

GIF is deliberately rejected even though OpenRouter can accept it: multi-frame interpretation is ambiguous and the MVP uses one consistent static-image contract across providers. PDF/HEIC remain unsupported.

### Gemini adapter

- Uses the installed official `google-genai` SDK and Pydantic structured output.
- Sends the prompt then the private inline image bytes at temperature zero.
- Model IDs are configuration, with an ordered fallback list.
- Maps authentication, unavailable model, rate limit, timeout, transport, and server failures to safe typed errors.
- Retries retryable failures with bounded exponential backoff and moves to the next configured model when appropriate.
- Detects safety/refusal outcomes and malformed/empty structured output.
- Never includes upstream response bodies, image content, or credentials in exception messages/logging.

The implementation matches Google's documented Pydantic schema/JSON response approach and image input behavior: [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output) and [Gemini image understanding](https://ai.google.dev/gemini-api/docs/image-understanding).

### OpenRouter adapter

- Uses `POST /api/v1/chat/completions` with prompt-first multipart content and a private base64 data URL.
- Requests strict `json_schema` output generated from the same Pydantic provider schema.
- Sets `provider.require_parameters=true` because structured-output support is endpoint-specific.
- Validates the envelope and structured output locally even when strict mode is requested.
- Maps HTTP/choice errors, authentication, model absence, rate limit, timeout, transport/server errors, content filtering, empty content, and malformed JSON to the common typed taxonomy.
- Returns the same provider-neutral result shape as Gemini.

This follows the official [OpenRouter image-input contract](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding), [structured-output contract](https://openrouter.ai/docs/guides/features/structured-outputs), and [chat API response contract](https://openrouter.ai/docs/api_reference/overview).

### Immutable extraction persistence

- Added `ExtractionAttemptRepository` with append-success, append-failure, and group-scoped list operations.
- Successful attempts retain provider/model, schema/prompt versions, structured original observation, normalized candidate, validation issues, and uncertainty/evidence.
- Failure attempts store only safe error code, retryability-derived status, provider/model, and version information—not sensitive upstream bodies.
- Attempt numbers increase per receipt.
- Added Alembic revision `2f4d9a10b7c1` with SQLite triggers that reject updates/deletes to `extraction_attempts`.
- Existing composite receipt/group foreign keys reject cross-group extraction attempts.

### Diagnostics and CLI compatibility

- Sensitive provider-bound image diagnostics are disabled by default.
- Opt-in diagnostics use UUID keys under a managed root and save only provider-bound bytes plus preprocessing metadata.
- The existing `extract_receipt.py` command retains its image/output/model/list/raw/enhance/grayscale/max-side options and now delegates to the provider-neutral modules.
- `--debug-dir` is now explicit opt-in instead of always writing receipt copies/raw extraction logs. This intentional privacy change is the main CLI behavior difference.
- Added `scripts/live_extraction_smoke.py`, which requires an explicit provider/model/image and prints only provider identifiers, review/issue status, and hashes—not extracted financial fields.
- Added `docs/LIVE_EXTRACTION_TESTS.md` with opt-in credential/privacy procedure and pass criteria.

### Configuration and dependencies

- Added provider selection, ordered Gemini model configuration, and optional OpenRouter model configuration.
- Added `google-genai`, `httpx`, and Pillow as explicit bounded project dependencies and updated the exact lock files with installed transitives.
- No credential is hardcoded. Provider factory creation fails safely when the selected provider's key/model is missing.

## Automated tests and acceptance evidence

Final command:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest
```

Final result:

```text
collected 73 items
73 passed in 2.68s
```

The plugin-autoload environment guard remains necessary on this workstation because of the unrelated global Hydra/ANTLR conflict documented in Step 01. No project plugin is disabled by this command.

| Step 02 acceptance area | Automated evidence |
|---|---|
| Persian/Arabic-Indic numerals | Both digit sets, grouping separators, accounts, IBAN, date, tracking, and amounts are normalized. |
| Rial/toman/unknown unit | Exact rial, exact toman conversion, and unknown-unit no-conversion/review behavior pass. |
| Printed and handwritten uncertainty | Printed certain fixture remains clean; handwritten uncertain amount preserves the candidate but sets field uncertainty and review. |
| Missing/invalid fields | Missing/malformed/zero amount, invalid IBAN, invalid date/time, unexpected account characters, and non-digit tracking are flagged deterministically. |
| Existing sample migration | `samples/sample.json` maps to the neutral schema without invoking AI or claiming OCR correctness. |
| Gemini contract | Structured config, recursively compatible Gemini schema, image request, parsed result, ordered model fallback, 429 retry, authentication, timeout, refusal, and malformed output pass with fakes. |
| OpenRouter contract | Auth/header, prompt-first private base64 image, strict JSON schema, required-parameter routing, usage/result parsing, 429 retry, timeout, refusal, and malformed envelope pass with `httpx.MockTransport`. |
| Shared provider interface | Both adapters return equal neutral candidate/raw structures for the same mocked observation. |
| Image preprocessing | Original-byte preservation/hash determinism, EXIF, transparency, resize/no-upscale, raw validation, damage fallback, MIME mismatch, corrupt/oversized/pixel-limit/unsupported files pass. |
| Managed diagnostics | Disabled mode writes nothing; enabled mode creates only managed relative UUID artifacts. |
| Extraction persistence | Successful/failure attempts, monotonic numbering, stored normalization/observation, cross-group rejection, and database update/delete rejection pass. |
| Migrations | Zero-to-head, head-to-base, recreate, trigger presence, and Alembic model-drift check pass with both revisions. |
| Credential independence | All provider responses/transports are fake/mock; the suite performs no external AI request. |

Additional final validation:

```text
compileall: passed
extract_receipt.py --help: passed
live_extraction_smoke.py --help: passed
alembic heads: 2f4d9a10b7c1 (head)
locked dependency dry-run resolution: passed (including SOCKS support)
credential-pattern scan outside ignored sensitive fixture/debug paths: 0 files
```

### Test-run history

The first Step 02 run collected 73 items: 70 passed and three image tests failed because the test helper passed `exif=None` to Pillow encoders and a resize fixture was too uniform, correctly triggering the production damage fallback. The helper was corrected to omit absent EXIF, generate meaningful contrast, and avoid accidental pytest collection.

The next run collected 72 items: 69 passed and three persistence tests failed because one helper call retained the old function name. That test-only reference was corrected. The full suite then passed 72/72 and was rerun successfully in the final gate. Production code was not weakened to make these fixtures pass.

The authorized live-verification follow-up exposed a Gemini API incompatibility with Pydantic's `additionalProperties` keyword. A no-image, non-financial schema probe identified the exact 400 response. The Gemini adapter now recursively removes only that unsupported provider keyword from its wire schema while local Pydantic validation remains strict. A regression test was added, the live smoke call then succeeded, and the complete suite passed 73/73.

Because the authorized route uses `socks5h`, the project now declares HTTPX's SOCKS extra and locks `socksio==1.0.0`; proxy support no longer depends on an unrelated global installation. The dependency review also corrected the lock from `websockets==16.0` to `15.0.1`, satisfying `google-genai==1.54.0`'s declared `<15.1` constraint.

## Live integration status

- **Gemini:** an explicitly authorized direct call succeeded on 2026-10-20 with `gemini-3.8-flash`, `samples/sample.jpg`, and the supplied SOCKS proxy. It verified credential use, proxy transport, image submission, provider-side structured schema acceptance, response parsing, and deterministic normalization. The safe script emitted only provider/model/request status, review/issue status, and hashes. It did not emit extracted financial fields. This single call is not a ground-truth OCR accuracy or handwriting evaluation.
- **OpenRouter:** adapter contract is mocked and tested. No API key is available, so authentication, selected endpoint capability, routing, billing, and live response behavior remain pending.

The safe opt-in procedure is in `docs/LIVE_EXTRACTION_TESTS.md`. Automated test success must not be interpreted as live handwriting/OCR accuracy.

## Files added or changed

### Extraction implementation

- `src/receipt_bot/extraction/__init__.py`
- `src/receipt_bot/extraction/errors.py`
- `src/receipt_bot/extraction/schema.py`
- `src/receipt_bot/extraction/image.py`
- `src/receipt_bot/extraction/prompt.py`
- `src/receipt_bot/extraction/provider.py`
- `src/receipt_bot/extraction/service.py`
- `src/receipt_bot/extraction/validation.py`
- `src/receipt_bot/extraction/legacy.py`
- `src/receipt_bot/extraction/diagnostics.py`
- `src/receipt_bot/extraction/providers/__init__.py`
- `src/receipt_bot/extraction/providers/common.py`
- `src/receipt_bot/extraction/providers/factory.py`
- `src/receipt_bot/extraction/providers/gemini.py`
- `src/receipt_bot/extraction/providers/openrouter.py`
- Reworked `extract_receipt.py` as the compatibility CLI.

### Persistence and configuration

- Added `migrations/versions/2f4d9a10b7c1_immutable_extraction_attempts.py`.
- Extended `src/receipt_bot/persistence/repositories.py`.
- Updated `src/receipt_bot/config.py`, `.env.example`, `pyproject.toml`, `requirements.lock`, and migration tests.

### Tests and operations

- `tests/test_extraction_image.py`
- `tests/test_extraction_validation.py`
- `tests/test_extraction_providers.py`
- `tests/test_extraction_persistence.py`
- `scripts/live_extraction_smoke.py`
- `docs/LIVE_EXTRACTION_TESTS.md`
- Updated `docs/PROJECT_PLAN.md` and added this report.
- Moved the six legacy sample artifacts from the repository root into `samples/` and added `samples/README.md`; tests now reference `samples/sample.json`.

Sample contents were not rewritten; only their repository paths changed. The local Google AI Studio credential variable in `.env` was renamed from `API_KEY` to the explicit `GEMINI_API_KEY` without printing or changing its secret value. No legacy debug artifact or financial database was modified or created.

## Limitations and deferred work

- No real OCR quality statement can be made for screenshots, printed paper, or handwriting without an authorized live evaluation set. Handwriting remains best-effort and review-first.
- Model IDs and OpenRouter endpoint capabilities can change. They are configurable and must be checked during a future live smoke test.
- The current service is synchronous. Step 03 must run provider work outside the Telegram update loop and use durable job states.
- Adapter retries occur in-process. Durable queued/retry recovery belongs to Telegram intake/job orchestration in Step 03.
- No receipt is converted into a payment, linked to a request, or counted. Those are Step 04 responsibilities.
- Diagnostic capture is managed and opt-in, but group authorization/retention integration begins in Steps 03 and 06.
- PDF, HEIC, animated GIF, and multi-page inputs are outside the MVP image contract.
- Provider raw structured observations are sensitive database content. Telegram/private-access enforcement is not present until Step 03/05.
- OpenRouter live testing remains blocked on its unavailable key; this does not block the credential-independent adapter milestone.

## Next milestone

After explicit authorization, implement **Step 03 / Day 3 — Telegram intake, isolation, authorization, and basic commands** only:

- async polling service and idempotent update/message/photo intake;
- durable extraction job orchestration outside the Telegram handler path;
- group registration/source traceability and receipt download/storage;
- `/start`, `/help`, and `/settings` foundations in Persian;
- server-side Telegram admin and group-accountant authorization;
- opaque, expiring, group/user-bound callback sessions and private detail handoff;
- exact receipt duplicate prevention and conservative duplicate candidates;
- authorization, replay, group-isolation, recovery, and Persian interaction tests.

Step 04 must not begin in that same execution.
