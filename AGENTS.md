# AGENTS.md — finappa

A single-user Telegram bot that extracts transactions from free-text messages via
an LLM, tracks them in SQLite, and answers `/summary`. Full design rationale:
`docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md`.

## Architecture in one paragraph

One Python process. `python-telegram-bot` long-polls Telegram (no webhook, no
HTTP server, no inbound port). SQLAlchemy over SQLite in WAL mode is the only
datastore. OpenAI Structured Outputs extracts transactions from free text.
PTB's `JobQueue` runs two daily jobs in-process: a recurring-transaction check and
an encrypted backup upload. Deployed as one Docker container with no `ports:` key.

## Invariants — do not violate these without updating the spec first

- **Money is `amount_cents: int`, always positive.** Direction is `type`
  (`EXPENSE`/`INCOME`/`REFUND`), never a sign. The only float is
  `extractor.TransactionItem.amount`, at the LLM boundary only.
- **EUR only.** Non-EUR rejects the whole message. No FX conversion anywhere.
- **`Transaction.ts` is always UTC.** Month boundaries are computed in
  `settings.timezone` (default `Europe/Berlin`) and converted to UTC before
  querying — see `app/analytics.py::month_bounds_utc`. Get this backwards and a
  purchase near midnight files under the wrong month.
- **Auth is one filter, not a module:** `filters.User(user_id=...)` on every
  handler. There is no `security.py` and there should never be one.
- **The ledger is the recurring-idempotency state.** `run_recurring_check`
  decides whether a rule already fired this month by querying `transactions`
  directly. Do not add a separate "runs" table — it can drift from reality; the
  ledger cannot.
- **All-or-nothing message validation.** One invalid item in a multi-item message
  rejects the whole message. Never log the valid items and drop the rest — the
  user's natural retry would then double-log them.
- **`/recat` backfills history.** It updates the merchant's stored category AND
  every past transaction for that merchant in one statement. Never implement it
  as compensating (reversal + re-entry) transactions — that pollutes every future
  `/summary` and `/export` with zero-sum rows.
- **No `ports:` key in `docker-compose.yml`, ever.** Docker writes its own
  iptables rules that bypass `ufw`; a published port is reachable even with the
  host firewall set to deny it.

## File map

| File | Responsibility |
|---|---|
| `app/config.py` | `pydantic-settings` — all env vars, one validation rule (backup key required if backup channel set) |
| `app/categories.py` | `CategoryName` StrEnum — the only source of truth for valid categories |
| `app/db.py` | SQLAlchemy models, WAL-mode engine, category seeding |
| `app/extractor.py` | OpenAI structured extraction + all-or-nothing boundary validation |
| `app/categorize.py` | Fuzzy merchant→category matching (`difflib`), `/recat` backfill |
| `app/analytics.py` | Berlin/UTC month-boundary math, `/summary` aggregation, money formatting |
| `app/recurring.py` | `recurring.yml` loader, idempotent daily recurring check |
| `app/backup.py` | `VACUUM INTO` snapshotting, Fernet encryption, local pruning |
| `app/handlers.py` | Message pipeline + all Telegram commands (thin async wrappers over plain functions) |
| `app/main.py` | Wires everything, registers the two `JobQueue` jobs, entrypoint |

## Common tasks

- **Add a category:** see `.claude/skills/add-category/SKILL.md`.
- **Add a command:** see `.claude/skills/add-command/SKILL.md`.
- **Deploy a change:** see `.claude/skills/deploy/SKILL.md`.
- **Restore from a backup:** see `.claude/skills/restore-backup/SKILL.md`.
- **Run tests:** `pytest -v` (no network required; the OpenAI client is stubbed
  everywhere except real usage).

## Deploy runbook (also in README.md)

```bash
ssh <host>
cd finappa
git pull
docker compose up -d --build
```

No CI/CD, no registry, no auto-deploy — this is a one-user bot deployed by hand
maybe twice a month. Do not add GitHub Actions deploy automation unless asked.

## Do not add

These were deliberately removed or rejected during design (spec §14) — do not
reintroduce them without a real, stated reason:

- FastAPI, Uvicorn, or any HTTP server — nothing serves HTTP.
- Caddy, a domain, or TLS termination — polling needs none of it.
- A webhook endpoint or `setWebhook` call.
- `Alembic` or any migration framework — one user, one DB, `create_all()` suffices.
- `thefuzz` — `difflib.get_close_matches` already does the one call needed.
- A separate cron container — `JobQueue` runs in-process.
- Google Drive backups — service accounts have no Drive storage quota.
- A regex fast-path before the LLM call — saves under a cent, adds a second code
  path to keep in sync.
- Any new dependency for something a few lines of stdlib already does.
