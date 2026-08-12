# AGENTS.md — finappa

A single-user Telegram bot that extracts transactions from free-text messages via
an LLM, tracks them in SQLite, and answers `/summary`. Full design rationale:
`docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md`.

## Architecture in one paragraph

One Python process. `python-telegram-bot` long-polls Telegram (no webhook, no
HTTP server on the bot). SQLAlchemy over SQLite in WAL mode is the only
datastore. OpenAI Structured Outputs extracts transactions from free text.
PTB's `JobQueue` runs two daily jobs in-process: a recurring-transaction check and
an encrypted backup upload. A sibling **stats** container serves a read-only
one-pager (Starlette + Basic Auth) with one published port. Deployed via Docker
Compose; the bot service still has no `ports:` key.

## Invariants — do not violate these without updating the spec first

- **Money is `amount_cents: int`, always positive.** Direction is `type`
  (`EXPENSE`/`INCOME`), never a sign. The only float is
  `extractor.TransactionItem.amount`, at the LLM boundary only.
- **EUR only.** Non-EUR rejects the whole message. No FX conversion anywhere.
- **`Transaction.ts` is always UTC.** Month boundaries are computed in
  `settings.timezone` (default `Europe/Berlin`) and converted to UTC before
  querying — see `app/analytics.py::month_bounds_utc`. Get this backwards and a
  purchase near midnight files under the wrong month.
- **Auth is one filter, not a module:** `filters.User(user_id=...)` on every
  message/command handler. The one exception is `CallbackQueryHandler` —
  `filters.User` does not compose with it, so `handle_category_confirmation`
  checks `query.from_user.id` manually as its first line, before
  `query.answer()`. Do not remove that guard. There is no `security.py` and
  there should never be one.
- **The ledger is the recurring-idempotency state.** `run_recurring_check`
  decides whether a rule already fired this month by querying `transactions`
  directly. Do not add a separate "runs" table — it can drift from reality; the
  ledger cannot.
- **Recurring rules fire on their exact day only, never catch up.**
  `day_of_month == today_local.day`, not `<=`. Deliberately reversed
  2026-08-11 from the original self-healing design after a rule fired 10
  days late on first deploy and read as a surprising backdated entry — the
  user was shown the trade-off (a rule's day passing during downtime now
  skips that month entirely) and chose it anyway. Do not revert to `<=`
  without asking first; see the design spec's §8.2 addendum for the full
  history.
- **All-or-nothing message validation.** One invalid item in a multi-item message
  rejects the whole message. Never log the valid items and drop the rest — the
  user's natural retry would then double-log them.
- **`/recat` backfills history.** It updates the merchant's stored category AND
  every past transaction for that merchant in one statement. Never implement it
  as compensating (reversal + re-entry) transactions — that pollutes every future
  `/summary` and `/export` with zero-sum rows.
- **Uncertain new-merchant categorization holds for confirmation, never
  guesses.** A new or merchant-less item where the LLM sets
  `category_confident: false` is never inserted until the user taps a
  category button (or "Don't log this") in Telegram. Known merchants and
  confident guesses log immediately, unchanged — see
  `app/handlers.py::process_message`.
- **Every `JobQueue` job needs `job_kwargs={"misfire_grace_time": None}`.**
  APScheduler's default grace window is 1 second; `run_once(..., when=0)`
  targets "right now," so any startup delay past 1s (normal — connecting to
  Telegram, etc.) causes the job to be silently *skipped entirely*, not run
  late — only a warning is logged. Bit us in production: the startup
  recurring check missed by 2.5s and never ran, so rent silently didn't get
  logged. Every job here is idempotent and safe to run whenever the process
  gets to it, so none should ever be droppable this way. See
  `app/main.py::build_application`.
- **`httpx`'s logger is pinned to `WARNING` in `_configure_logging`
  (`app/main.py`), never left at `INFO`.** Telegram's Bot API puts the bot
  token in the request URL itself (no Authorization header), and `httpx`
  logs full request URLs at `INFO` — every `getMe`/`getUpdates`/`sendMessage`
  call would otherwise write the live token into persisted container logs.
  Confirmed live in production before this was caught. Don't raise this
  logger back to `INFO` without redacting the URL first.
- **No `ports:` on the bot service, ever.** Docker writes its own
  iptables rules that bypass `ufw`; a published port on the bot would be
  reachable even with the host firewall set to deny it. The **stats** sibling
  service is the sole exception — it may publish exactly one host port for the
  read-only one-pager (HTTP Basic Auth). Never assume the host is dedicated to
  finappa alone.

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
| `app/ledger.py` | Transaction list/delete/edit helpers and backdating timestamp math |
| `app/main.py` | Wires everything, registers the two `JobQueue` jobs, entrypoint |
| `stats/` | Read-only stats one-pager (Starlette); sibling compose service with Basic Auth |

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
- A DB-backed pending-confirmation table — the in-memory
  `bot_data["pending_confirmations"]` dict is deliberate; losing it on
  restart only costs a resend, never data (see
  `docs/superpowers/specs/2026-08-11-category-confirmation-design.md` §5.1).
- FastAPI (or any HTTP server) **inside the bot process** — the stats
  one-pager is a sibling container (`python -m stats`), not an inbound
  route on the Telegram app.
