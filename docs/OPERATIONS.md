# Production Operations Runbook

## 1. Supported deployment

The MVP deployment target is one Linux host running Docker Engine with Docker Compose. The process uses Telegram long polling, one SQLite database in WAL mode, one managed receipt-image directory, and one bot replica. Horizontal replicas and shared/network SQLite filesystems are unsupported.

Persistent locations:

- `/app/data/receipt_bot.db`: authoritative SQLite database;
- `/app/data/receipts`: managed original receipt images;
- `/app/backups`: validated database backups, bind-mounted to host `./backups`.

Keep the host, Docker data root, and backup destination encrypted and access-controlled. Copy validated backups to an encrypted off-host destination; the local backup directory alone is not disaster recovery.

## 2. Telegram and BotFather setup

1. Create the bot with BotFather and place the token only in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Use `/setprivacy` in BotFather and disable privacy mode. Otherwise Telegram will not deliver ordinary financial conversation messages to the bot.
3. Add the bot to each intended group. Do not grant message deletion, user banning, ownership, or other unrelated permissions. Validate in staging that the bot can receive messages, send messages/buttons, download files, and inspect member/admin status.
4. Add the trusted administrator's Telegram username to `RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES`, then restart the service. A leading `@` is optional and matching is case-insensitive.
5. That same allowlisted account runs `/start` in the intended group while it is a current group administrator. The bot checks both conditions server-side, records the normalized username as the authorization anchor, and retains the numeric Telegram User ID for audit traceability.
6. The administrator uses `/settings` to add or revoke authorized accounting reviewers.
7. Run `/help` and verify the Persian command list and `پشتیبانی ربات` link to [@amixrmz](https://t.me/amixrmz), the support contact and developer.

After activation, ordinary group members may submit financial messages and receipts. Reporting, review, settings, and cleanup retain their existing administrator/accountant checks. Removing the recorded username from the environment allowlist immediately blocks the group's commands, callbacks, intake, recovered updates, downloads, AI extraction, and materialization. To transfer authorization, add the new trusted username, remove the old username, restart, and have the new allowlisted current administrator run `/start`. Existing accounting data is preserved throughout.

The bot cannot retrieve group history from before it joined. Reports cover only captured or deliberately imported records and must not be presented as complete for earlier periods.

## 3. Secrets and provider configuration

Copy `.env.example` to `.env`. Never place credentials in `compose.yaml`, source files, screenshots, issue trackers, or routine logs. Do not run `docker compose config` without `--quiet` in recorded terminals because Compose expands interpolated secrets in its rendered output.

Required:

- `TELEGRAM_BOT_TOKEN`;
- `RECEIPT_BOT_ALLOWED_TELEGRAM_USERNAMES`, a comma-separated list of Telegram usernames permitted to authorize groups; leading `@` is optional;
- `GEMINI_API_KEY` when `RECEIPT_BOT_EXTRACTION_PROVIDER=gemini`;
- `OPENROUTER_API_KEY` when `RECEIPT_BOT_EXTRACTION_PROVIDER=openrouter`.

The service fails closed if the allowlist is missing, empty, malformed, or contains duplicate usernames after case normalization. Existing active groups without the username authorization marker—such as groups created before this feature—remain blocked until an allowlisted current administrator reactivates them with `/start`. Provider selection and model IDs are configuration. Restart after a configuration change. Provider API consumption, Telegram-independent hosting, off-host backup storage, and network proxy costs are operating costs outside the development budget.

Telegram usernames are mutable and can potentially be reassigned. This is weaker than numeric-ID authorization and cannot be fully mitigated by the Bot API. Confirm account ownership before allowlisting. When a trusted administrator changes username, replace the old username in the environment, restart, and reactivate every intended group with `/start`.

If an outbound proxy is required, configure `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` only in the protected runtime environment. Do not commit proxy credentials.

## 4. Deploy and upgrade

```bash
cp .env.example .env
mkdir -p backups
sudo chown 10001:10001 backups
# edit .env securely
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail 100 receipt-bot
```

The application startup path performs these checks before polling:

1. create the data and receipt-storage directories;
2. verify receipt storage is writable;
3. run `alembic upgrade head`;
4. run SQLite integrity and foreign-key checks;
5. ensure the database revision equals the packaged migration head.

An invalid configuration, failed migration, corrupt database, outdated schema, or unavailable storage causes startup to fail closed.

For upgrades:

```bash
docker compose exec receipt-bot python scripts/manage_runtime.py backup /app/backups/pre-upgrade-YYYYMMDD-HHMM.db
docker compose pull
docker compose build --pull
docker compose up -d
docker compose exec receipt-bot python scripts/manage_runtime.py check
```

Replace the timestamp placeholder with a unique value. Backup creation refuses to overwrite an existing file.

## 5. Health, logs, and graceful shutdown

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' tel_receipts_agent-receipt-bot-1
docker compose exec receipt-bot python scripts/manage_runtime.py check
docker compose logs --tail 200 receipt-bot
```

The container health check validates database integrity, foreign keys, migration revision, and the receipt-storage directory every 30 seconds. Container death is independently visible through Docker state.

Logs are JSON on standard error. Credential-shaped content in messages/exceptions is redacted, context keys containing tokens/API keys/secrets/passwords are redacted, and verbose HTTP transport logging is suppressed because Telegram embeds its token in request URLs. The Compose `local` log driver rotates at 10 MB with five files. Financial message bodies, receipt fields, account identifiers, and image bytes must never be added to routine application logs.

`docker compose stop` sends `SIGTERM`. Polling stops immediately while an in-flight worker receives up to 30 seconds to commit. If interrupted, the single-instance startup recovery immediately returns abandoned download/extraction claims to their durable queues; database constraints and source IDs prevent duplicate materialization.

## 6. Database backup

Use the provided online backup command rather than copying `receipt_bot.db`, `-wal`, and `-shm` files independently:

```bash
docker compose exec receipt-bot python scripts/manage_runtime.py backup /app/backups/receipt-bot-YYYYMMDD-HHMM.db
```

The command uses SQLite's backup API, validates `integrity_check` and foreign keys, calculates SHA-256, writes through a temporary file, and atomically publishes the result. It refuses an existing destination and never modifies the live database.

Recommended schedule:

- database backup at least daily and before every upgrade;
- encrypted off-host copy after every successful backup;
- retention appropriate to the client's privacy/accounting policy;
- restore rehearsal at least monthly.

Receipt images are separate files. For full disaster recovery, also snapshot `/app/data/receipts`. Stop the bot for a fully coordinated database-and-image snapshot. Missing restored images do not erase accounting records, but detail views will indicate that the original file is unavailable.

## 7. Restore rehearsal and incident restore

Always rehearse into a new destination:

```bash
docker compose run --rm receipt-bot python scripts/manage_runtime.py restore \
  /app/backups/receipt-bot-YYYYMMDD-HHMM.db /app/backups/restore-check.db
```

The restore command validates both source and restored files and refuses to overwrite anything. Point a temporary isolated environment at `restore-check.db`, run `check`, and compare a known historical `/reports` period and manifest before accepting the backup.

For a production recovery:

1. stop the bot;
2. preserve the damaged database plus its WAL/SHM files in an incident directory—do not delete them;
3. restore a validated backup to a new empty `receipt_bot.db` path;
4. restore the receipt-image snapshot if available;
5. run `python scripts/manage_runtime.py check`;
6. start one bot instance and inspect `/pending`, `/reports`, and logs.

Do not restore over a running or existing database. The tool intentionally has no force-overwrite option.

## 8. Accounting operation

- Reply to a captured participant message with `/request`, enter `amount unit | note`, inspect the confirmation, and press the final button.
- Use `/promise` the same way for a promise. A promise is reported separately and never counted as submitted/confirmed payment.
- Receipt text/images enter the extraction queue automatically when conservatively recognized.
- Use `/pending` or `/verify` to confirm, reject, correct, deduplicate, and allocate evidence.
- Use `/reports` as the authoritative user-facing reconciliation view.
- Use `/cleanup` only after checking its count, cutoff, estimated bytes, and second confirmation.

Never describe documentary confirmation as independent bank settlement. Never describe one request's difference as a person's overall debt.

## 9. Retention and privacy

The default image retention is 183 days and is configurable per group through `/settings`. `/cleanup` removes only eligible managed image files. Telegram source references, receipt metadata, extraction attempts, requests, promises, payments, allocations, reviews, revisions, reports, and audits remain.

Raw completed Telegram update envelopes are cleared; the normalized source messages needed for traceability remain. Sensitive detail drill-down belongs in authorized private chat where offered. Restrict host, volume, backup, and Telegram-group access to personnel with an accounting need.

The repository's `samples/` and `debug/` directories are excluded from container builds. Treat them as sensitive until the owner confirms they are synthetic or authorized; do not upload them to source control or deployment systems.

## 10. Incident response

- **Telegram token exposed:** revoke/regenerate with BotFather, update `.env`, restart, and review access/logs.
- **Provider key exposed:** revoke at the provider, replace `.env`, restart, and review usage/costs.
- **Allowlisted administrator compromised, renamed, or removed:** remove the username from the allowlist, restart immediately, review authorized groups and audit events, then reactivate only approved groups through a different allowlisted current administrator.
- **Extraction outage:** keep queued jobs; verify provider/model/proxy; do not manually invent extracted values. Accountants may process pending items through review after evidence is available.
- **Database health failure:** stop the bot, preserve all database/WAL/SHM files, copy logs, and restore into a separate location.
- **Disk pressure:** create a validated backup, inspect volume use, then use `/cleanup`; never delete database rows or arbitrary receipt paths.
- **Incorrect accounting decision:** use correction/reopen workflows so immutable extraction and revision/audit history remain.

## 11. Staging acceptance

Run [STAGING_ACCEPTANCE.md](STAGING_ACCEPTANCE.md) in a dedicated non-production Telegram group with synthetic data. Record tester, date, bot version/image digest, provider/model, and evidence for every item. A check not actually performed remains pending.
