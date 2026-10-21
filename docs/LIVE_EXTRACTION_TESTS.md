# Opt-in Live Extraction Smoke Tests

Live AI calls are deliberately excluded from the deterministic test suite. They consume provider credit and send image content to an external service. Run these checks only with a synthetic receipt or an image whose use and disclosure have been explicitly authorized.

## Prerequisites

1. Install the locked project environment.
2. Select a current vision-capable model that supports structured output.
3. Set the chosen provider credential in the process environment. Never put a real key in a command, source file, report, or terminal transcript.
4. Review the provider's data-retention/privacy setting for financial images.

If the local SOCKS proxy is required, configure it in the invoking process before the call:

```powershell
$env:HTTP_PROXY  = 'socks5h://127.0.0.1:10808'
$env:HTTPS_PROXY = 'socks5h://127.0.0.1:10808'
$env:ALL_PROXY   = 'socks5h://127.0.0.1:10808'
```

The smoke script prints only provider/model/request identifiers, review/issue status, and image hashes. It does not print extracted names, accounts, references, descriptions, or amounts and does not enable diagnostic image capture.

## Direct Gemini

```powershell
$env:GEMINI_API_KEY = '<set securely outside captured history where possible>'
python scripts/live_extraction_smoke.py gemini gemini-3.8-flash path/to/synthetic-receipt.png
Remove-Item Env:GEMINI_API_KEY
```

Pass criteria:

- exit code zero;
- provider is `gemini`;
- returned model/request information is present when the provider supplies it;
- issue codes and `requires_review` agree with the deliberately known synthetic fields;
- routine logs/output contain no key or extracted financial fields.

Current status: **passed again on 2026-10-21 during Step 07 for transport, image submission, and structured-response validation** using direct Gemini, `gemini-3.8-flash`, the configured SOCKS proxy, and the explicitly authorized `samples/sample.jpg` fixture (SHA-256 `a928b3f5e70f7abb3dc7a0a97dad2acbf628d8dc8efb01379ffa2b91fce1292c`). The command exited zero, returned a provider request identifier, required no review for that fixture, and emitted no extracted financial fields or credential. This smoke call does not ground-truth general OCR or handwriting accuracy.

## OpenRouter

```powershell
$env:OPENROUTER_API_KEY = '<set securely outside captured history where possible>'
python scripts/live_extraction_smoke.py openrouter google/gemini-3.8-flash path/to/synthetic-receipt.png
Remove-Item Env:OPENROUTER_API_KEY
```

Before running, confirm on OpenRouter that the selected endpoint accepts image input and the `structured_outputs` parameter. The adapter requires supported parameters during routing.

Current Step 02 status: **pending because no OpenRouter API key is available**. Credential-independent mocked adapter tests are the only OpenRouter verification in Step 02.

## Recording a future result

Record the date, provider/model, synthetic fixture identifier/hash, command exit status, observed issue codes, and whether output/log redaction was checked in the current step or operations report. Never paste keys, raw provider bodies, receipt content, or account identifiers into the report.
