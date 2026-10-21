# Contributing

Thank you for improving the Telegram Receipt & Financial Reconciliation Bot. Financial correctness, source traceability, and privacy take priority over convenience.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-test.lock
Copy-Item .env.example .env
python scripts/manage_runtime.py prepare
```

Use Python 3.12 or 3.13. Never commit `.env`, databases, receipt images, provider responses, debug captures, or logs containing financial identifiers.

## Change workflow

1. Create a focused branch from `main`.
2. Add or update deterministic tests alongside the change.
3. For schema changes, add an Alembic migration and test both a fresh database and an upgrade.
4. Preserve original extraction observations and audit history; corrections must be additive.
5. Run the validation commands below before opening a pull request.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
python -m compileall -q src scripts tests
python scripts/security_scan.py
docker build --tag telegram-receipt-bot:local .
```

## Test-data policy

- Prefer small synthetic fixtures under `tests/fixtures/`.
- Keep local receipt images under the ignored `samples/` directory.
- Do not use real names, cards, IBANs, references, or receipt images in tests or documentation.
- Live AI and Telegram checks are opt-in. Use only synthetic or explicitly authorized data, verify authorization and group isolation, and complete them in a non-production group before sign-off.

## Pull-request checklist

- Financial calculations still use deterministic integer arithmetic.
- Group isolation and server-side authorization are tested.
- New logs and errors do not expose credentials or financial fields.
- User-visible Persian text clearly distinguishes evidence from confirmed settlement.
- Documentation and migrations are updated when behavior or configuration changes.

The repository does not currently declare an open-source license. Obtain the owner's approval before redistributing the code.
