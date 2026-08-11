# Telegram Personal Finance Tracker — Design

Date: 2026-08-10
Status: Approved

## 1. Overview

A single-user Telegram bot that turns free-text messages ("30€ in Rewe and 15€ for
fuel") into structured ledger entries in SQLite, answers month-to-date questions,
handles recurring transactions, and backs itself up daily.

Deployed as **one Docker container on any always-on Linux host**. No inbound ports,
no domain, no TLS, no reverse proxy. The host is deliberately interchangeable
(§10.2).

### Goals

- Log one or many transactions from a single natural-language message.
- Keep category assignment stable across months for the same merchant.
- Answer `/summary` for the current month with correct arithmetic.
- Insert recurring items (rent, salary, ETF) exactly once per month, self-healing
  across container downtime. **Superseded 2026-08-11 — see §8.2 addendum:** a
  rule now only fires on its exact configured day; self-healing across
  downtime was deliberately removed by explicit request.
- Survive VPS loss with at most 24 hours of data lost.

### Non-goals (v1)

- Multi-user. The whitelist is one user ID.
- Multi-currency. EUR only; anything else is rejected.
- Backdating. A transaction's timestamp is the time the message arrived.
  "Yesterday I spent 30 at Rewe" logs as today.
- Receipt photos, voice notes.
- Editing a transaction's amount. `/undo` and re-send.
- A web UI. `/summary` is the interface. Revisit after a month of real use.
- Schema migrations. `create_all()` only; schema changes are manual `ALTER TABLE`.

## 2. Architecture

### 2.1 Long polling, not webhooks

The bot calls Telegram (`getUpdates`), Telegram never calls the bot.

Rationale: a webhook forces a public HTTPS endpoint, which forces a domain,
a certificate, a reverse proxy, an open port, and a secret-token header to
distinguish Telegram from strangers who find the endpoint. Polling needs none of
it. Latency is equivalent — long polling returns the instant a message arrives.

Eliminated by this decision: Caddy and `Caddyfile`, FastAPI and Uvicorn, the
domain and Let's Encrypt, all inbound ports, `X-Telegram-Bot-Api-Secret-Token`
validation, `setWebhook` registration, `update_id` deduplication (PTB tracks the
offset), the `/api/v1/trigger-recurring` endpoint, `INTERNAL_CRON_SECRET`, and the
separate cron container.

Accepted cost: one idle outbound HTTPS connection; only one instance may run at a
time (a second gets HTTP 409 from Telegram).

### 2.2 Authorization is a filter, not a module

`telegram.ext.filters.User(user_id=settings.allowed_telegram_user_id)` is attached
to every handler. Unauthorized updates are dropped by the library before any
handler code runs.

There is no `security.py`. Writing custom auth here would be strictly worse than
the library primitive.

### 2.3 Process shape

One Python process:

- `Application` with polling, running the handlers.
- `JobQueue` (APScheduler, via `python-telegram-bot[job-queue]`) running the two
  scheduled jobs in the same process.

SQLite lives on a host bind mount, WAL mode enabled at connection time.

## 3. File layout

```
finappa/
├── app/
│   ├── __init__.py
│   ├── main.py          # Application build, handler + job registration, entrypoint
│   ├── config.py        # pydantic-settings
│   ├── db.py            # engine, session factory, 4 models, WAL pragma, seeding
│   ├── categories.py    # CategoryName StrEnum — single source of truth
│   ├── handlers.py      # text messages, /summary /undo /export /recat /help
│   ├── extractor.py     # OpenAI structured output
│   ├── categorize.py    # difflib merchant match + upsert
│   ├── analytics.py     # SQL aggregations, month-boundary math
│   ├── recurring.py     # recurring.yml → idempotent inserts
│   └── backup.py        # VACUUM INTO → Telegram channel → local prune
├── tests/
│   ├── conftest.py
│   ├── test_extractor.py
│   ├── test_categorize.py
│   ├── test_analytics.py
│   ├── test_recurring.py
│   ├── test_undo.py
│   └── test_backup.py
├── data/                # gitignored, bind-mounted from host
├── recurring.yml
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── README.md
├── AGENTS.md
├── CLAUDE.md            # one-line pointer to AGENTS.md
├── .cursor/rules/finappa.mdc
└── .claude/skills/
    ├── deploy/SKILL.md
    ├── add-category/SKILL.md
    ├── restore-backup/SKILL.md
    └── add-command/SKILL.md
```

Deviations from the originally requested layout, and why:

- `db/session.py` + `db/models.py` → one `app/db.py`. Four small models plus an
  engine is roughly 120 lines.
- `app/bot/` package → flat `app/handlers.py`. One module does not need a package.
- `services/gdrive.py` → `app/backup.py` (Telegram-based, see §8.1).
- `services/cache.py` → `app/categorize.py`. It is not a cache; it is a
  normalization table (see §6.2).

## 4. Data model

SQLAlchemy 2.x declarative models. `create_all()` at startup.

```
Category
    id            INTEGER PK
    name          TEXT UNIQUE NOT NULL      -- seeded from Category StrEnum

Merchant
    id            INTEGER PK
    name          TEXT UNIQUE NOT NULL      -- normalized: lowercased, stripped
    display_name  TEXT NOT NULL             -- as first seen, for confirmations
    category_id   INTEGER FK -> Category
    created_at    DATETIME NOT NULL

Transaction
    id                 INTEGER PK
    ts                 DATETIME NOT NULL     -- UTC, indexed
    amount_cents       INTEGER NOT NULL      -- always positive
    type               TEXT NOT NULL         -- EXPENSE | INCOME | REFUND
    merchant_id        INTEGER FK -> Merchant, NULL
    category_id        INTEGER FK -> Category, NOT NULL
    notes              TEXT NULL
    source             TEXT NOT NULL         -- 'message' | 'recurring'
    source_update_id   INTEGER NULL, indexed -- groups one message's rows
    recurring_rule_id  INTEGER FK -> RecurringRule, NULL, indexed

RecurringRule
    id            INTEGER PK
    key           TEXT UNIQUE NOT NULL      -- stable slug from recurring.yml
    label         TEXT NOT NULL
    amount_cents  INTEGER NOT NULL
    type          TEXT NOT NULL
    category_id   INTEGER FK -> Category
    day_of_month  INTEGER NOT NULL          -- 1..28 (see §8.3)
    active        BOOLEAN NOT NULL DEFAULT 1
```

### 4.1 Money

`amount_cents` is an **integer**, always **positive**. Direction is carried by
`type`. Floats never touch persisted money; the only float is `TransactionItem.amount`
at the LLM boundary, converted with `round(amount * 100)`.

Arithmetic convention:

```
expenses = SUM(EXPENSE) - SUM(REFUND)
income   = SUM(INCOME)
net      = income - expenses
```

A refund reduces the expense total in its own category rather than counting as
income.

### 4.2 Time

SQLite has no timezone type. **All `ts` values are UTC.** All user-facing
boundaries are computed in `settings.timezone` (default `Europe/Berlin`) and
converted to UTC before querying:

```python
start_local = datetime(y, m, 1, tzinfo=ZoneInfo(tz))
start_utc   = start_local.astimezone(timezone.utc)
```

Getting this backwards files a 00:30-on-the-1st purchase into the previous month.
This is explicitly covered by a test.

## 5. LLM extraction contract

### 5.1 Schema

The enum is named `CategoryName` to avoid colliding with the `Category`
SQLAlchemy model in §4.

```python
class CategoryName(StrEnum):
    GROCERIES = "Groceries"
    DINING = "Dining"
    TRANSPORT = "Transport"
    FUEL = "Fuel"
    HOUSING = "Housing"
    UTILITIES = "Utilities"
    HEALTH = "Health"
    SHOPPING = "Shopping"
    ENTERTAINMENT = "Entertainment"
    SUBSCRIPTIONS = "Subscriptions"
    TRAVEL = "Travel"
    INVESTMENT = "Investment"
    SALARY = "Salary"
    OTHER = "Other"

class TransactionItem(BaseModel):
    amount: float
    currency: str          # required — see below
    merchant: str          # "" when there is none (e.g. salary)
    category: CategoryName
    type: Literal["EXPENSE", "INCOME", "REFUND"]
    notes: str | None

class ExtractionResult(BaseModel):
    transactions: list[TransactionItem]
```

`CategoryName` is the single source of truth: it seeds the `categories` table *and*
becomes a JSON-schema `enum`, making an off-list category structurally impossible
rather than merely discouraged.

**`currency` has no default on purpose.** OpenAI Structured Outputs strict mode
rejects schemas with default values, so `currency: str = "EUR"` would fail at the
API. The model always fills it; we validate downstream.

### 5.2 Call

Use the SDK's structured-output parse helper with `response_format=ExtractionResult`
and `OPENAI_MODEL` (default `gpt-4o-mini`). The helper's location has moved between
SDK versions (`client.beta.chat.completions.parse` → `client.chat.completions.parse`);
verify against the pinned version at implementation time rather than assuming.

Failure handling: any exception (timeout, rate limit, refusal, parse error) is
caught and answered with a short message to the user naming the failure. Silently
dropping a message is not acceptable — the user would believe the expense was
logged.

### 5.3 Validation at the boundary

Applied to every extracted item before anything is written:

- `currency != "EUR"` → **reject the entire message**, log nothing, reply
  explaining.
- `amount <= 0` or `amount > 1_000_000` → reject the message. A hallucinated
  `30000` for "30€" must not reach the ledger.
- `transactions` empty → reply that nothing was found; log nothing.

**Why all-or-nothing rather than logging the valid items:** if one item of three
is rejected, the user's natural response is to retype the corrected message — which
would log the other two a second time. Rejecting the whole message makes
"just re-send it" always the correct action.

To keep re-sending cheap, the rejection reply quotes back what was parsed:

```
Nothing logged — 'fuel' came back as USD, and I only handle EUR.
I read: 30,00 € Rewe (Groceries) · 15,00 USD fuel
```

## 6. Message pipeline

`MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ALLOWED_ID))`

1. Extract via LLM (§5).
2. Validate all items (§5.3). Any failure aborts the whole message.
3. For each item, resolve the merchant and category (§6.2).
4. Insert all rows in one transaction, sharing the update's `update_id` as
   `source_update_id` and `source='message'`.
5. Reply with an itemized confirmation plus month-to-date net:

   ```
   Logged 2:
   • 30,00 € · Rewe · Groceries
   • 15,00 € · Fuel
   Net this month: −412,50 €
   ```

### 6.1 Concurrency

PTB processes updates sequentially by default. The SQLite engine uses
`check_same_thread=False` with WAL enabled. The JobQueue runs in the same process,
so the daily jobs and message handling can interleave — WAL handles this, and
writes are short.

### 6.2 Merchant normalization (`categorize.py`)

Not a cost optimization. `gpt-4o-mini` on a 20-word message costs a small fraction
of a cent; the merchant table exists so that **the same shop always lands in the
same category**.

```
normalized = merchant.strip().lower()
match = difflib.get_close_matches(normalized, known_names, n=1, cutoff=0.85)
if match:  use that merchant's stored category_id   # overrides the LLM
else:      create Merchant(normalized, llm_category) # LLM's choice becomes canonical
```

`difflib` is stdlib. `thefuzz` is not needed for a one-line call.

The regex fast-path from the original spec ("Spent <amount> at <merchant>" → skip
the LLM) is dropped: it only matches inputs nobody types, saves under a cent, and
creates a second code path to keep in sync with the first.

### 6.3 The poisoned-merchant problem

Consequence of §6.2: the LLM's *first* categorization of a merchant becomes
canonical forever. One bad guess ("Rewe" → `Dining`) silently mis-files every
future purchase there, and `/undo` cannot fix it because it deletes transactions,
not mappings.

Escape hatch: **`/recat <merchant> <category>`**. It repoints the stored mapping
**and backfills history** in one statement:

```sql
UPDATE transactions SET category_id = :new WHERE merchant_id = :merchant
```

Every past summary becomes correct rather than permanently skewed. The reply
reports the count: `Rewe → Groceries, 47 past transactions updated.`

Compensating transaction pairs were considered and rejected: a category change is
not a money movement, so the pair nets to zero and clutters `/summary` and
`/export` with phantom line items forever. The audit trail an in-ledger correction
would preserve already exists — yesterday's backup snapshot holds the previous
categorization.

## 7. Commands

| Command | Behavior |
|---|---|
| `/summary` | Current month: income, expenses (net of refunds), net, and a per-category breakdown. One `GROUP BY`. |
| `/undo` | Deletes every row sharing the most recent non-null `source_update_id`. Removes the whole message, not one line. Never touches `source='recurring'` rows. Replies with what was removed. |
| `/export` | All transactions as CSV built with the stdlib `csv` module into an in-memory buffer, sent as a document. |
| `/recat` | `/recat <merchant> <category>` — repoint a merchant's stored category and backfill all past transactions for that merchant (§6.3). Validates the category against the enum; lists valid ones on error. |
| `/help` | Usage. Also bound to `/start`. |

## 8. Scheduled jobs

Both registered on `JobQueue`. No cron, no HTTP endpoint, no shared secret.

### 8.1 Backup — 03:30 daily (`Europe/Berlin`)

1. `VACUUM INTO '/app/data/backup-YYYY-MM-DD.db'`.
   **Not `cp`** — copying a live WAL-mode database yields a corrupt snapshot.
2. Encrypt the snapshot with `cryptography.fernet.Fernet` using
   `BACKUP_ENCRYPTION_KEY`, producing `backup-YYYY-MM-DD.db.enc` (§13.2).
3. Send the encrypted file to `BACKUP_CHANNEL_ID` via `send_document`, with a
   caption carrying the date, row count, and file size.
4. Prune local snapshots to the newest 7 (the plaintext `.db` snapshots stay local
   only; the `.enc` is deleted after a successful send).

If `BACKUP_CHANNEL_ID` is unset, the local snapshot still runs and the send is
skipped.

Fernet reads the whole file into memory. At a few megabytes this is irrelevant; if
the database ever reaches hundreds of megabytes, switch to chunked streaming.

**Why Telegram, not Google Drive:** a Google service account has zero Drive storage
quota. Uploading to a folder shared from a personal account fails with
`storageQuotaExceeded` unless a Workspace Shared Drive is involved. The bot token
already exists, Telegram cloud storage is unlimited, and the 50 MB per-file bot
upload cap is irrelevant for a database measured in hundreds of kilobytes.

**No remote retention policy.** `deleteMessage` only works on messages sent less
than 48 hours ago, so a job can never delete an older backup. A dedicated private
channel keeps the clutter out of the main chat; manual bulk-delete in the app is
always available.

### 8.2 Recurring check — 06:00 daily, and once at startup

Runs **daily, not monthly**, on purpose: a monthly job that fires on the 1st while
the container is down silently skips that month's rent. A daily idempotent check
self-heals within 24 hours.

For each active rule where `day_of_month <= today_local.day`:

```sql
SELECT 1 FROM transactions
WHERE recurring_rule_id = :id AND ts >= :month_start_utc AND ts < :next_month_utc
```

No row → insert with `source='recurring'` and push a Telegram notification.

**The ledger is the idempotency state.** There is no separate "runs" table to drift
out of sync with reality.

**Addendum (2026-08-11) — self-healing removed by explicit request.** The
`day_of_month <= today_local.day` condition above is **no longer accurate**.
`run_recurring_check` (`app/recurring.py`) now uses `day_of_month ==
today_local.day` — a rule fires only on its exact day, never catching up
later in the month. This surfaced during initial deployment: rent's
`day_of_month: 1` fired on day 11 (the day the bot was actually set up,
exactly as self-healing was designed to do), and after seeing that in
practice, the user preferred no "surprise backdated-feeling" entry over the
resilience — they were shown the downtime-skips-the-month trade-off
explicitly and accepted it. The daily 06:00 schedule and the ledger-based
idempotency check (both above) are unchanged; only the comparison operator
changed. Current behavior, invariants, and the reasoning trail live in
`AGENTS.md` and `app/recurring.py`'s inline comment, not here — this
addendum exists so this section stops being read as current truth.

### 8.3 `recurring.yml`

Read at startup and upserted into `recurring_rules` by `key`. Ships with commented
examples.

```yaml
- key: rent
  label: Rent
  amount: 1200.00
  type: EXPENSE
  category: Housing
  day_of_month: 1
```

`day_of_month` is constrained to **1..28** so no rule can vanish in February.

## 9. Configuration

`pydantic-settings`, loaded from `.env`.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required |
| `ALLOWED_TELEGRAM_USER_ID` | — | required, int |
| `OPENAI_API_KEY` | — | required |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `BACKUP_CHANNEL_ID` | none | negative int; backups are local-only if unset |
| `BACKUP_ENCRYPTION_KEY` | — | required when `BACKUP_CHANNEL_ID` is set; urlsafe base64 Fernet key |
| `TIMEZONE` | `Europe/Berlin` | month boundaries and job fire times |
| `DB_PATH` | `/app/data/finappa.db` | |
| `LOG_LEVEL` | `INFO` | |

Startup fails loudly on a missing required variable rather than erroring on the
first message.

## 10. Deployment

`docker-compose.yml` is one service:

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
```

No `ports:` key. Nothing is exposed.

`Dockerfile`: single-stage `python:3.12-slim`, non-root user, `pip install .`,
`CMD ["python", "-m", "app.main"]`. A multi-stage build buys nothing for a pure-Python
app with no compile step. No `apt install sqlite3` — `VACUUM INTO` runs through the
stdlib `sqlite3` module.

Deploy: `ssh` in, `git pull && docker compose up -d --build`. Documented in
AGENTS.md and the `deploy` skill.

### 10.1 Host requirements

~150 MB RAM, negligible CPU, a disk that survives reboots, outbound HTTPS, and
Docker. Nothing else. Any 512 MB VM or single-board computer is oversized.

The design is deliberately host-portable, and three earlier decisions are what make
it so:

- **Polling** means no static IP, no port forwarding, no DNS, no domain. Works
  behind home NAT and CGNAT.
- **The daily idempotent recurring check** (§8.2) means the ledger, not a
  separate table, is what decides "already logged." **Self-healing across
  downtime was removed 2026-08-11 (§8.2 addendum)** — a day of downtime on a
  rule's exact day now skips that rule for the month, by explicit request.
- **Daily encrypted backups** (§8.1) cap host loss at 24 hours.

Migration is: copy `data/`, copy `.env`, `docker compose up -d --build`.

### 10.2 Host decision (2026-08-10)

**Cloud for now. Oracle Cloud Always Free, deliberately NOT upgraded to
Pay-As-You-Go.** Oracle refuses to provision beyond free-tier limits, so an
unexpected bill is impossible by construction — which is the requirement, not
merely a preference.

Accepted cost: Oracle may *stop* (not delete) instances idling below 10% CPU and
10% network over 7 days, and this bot is idle by design. The boot volume persists;
recovery is restarting the instance. If it happens more than twice, move to the
fallback rather than converting to Pay-As-You-Go — PAYG removes reclamation but
reintroduces billing exposure.

**Fallback: a prepaid annual budget VPS** (~$15/year). Prepayment makes overage
structurally impossible, with no reclamation policy to trust.

**Rejected: Hetzner.** [Prices rose on 15 June 2026](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/);
the cheapest CX23 is €5.49/mo excl. VAT and excl. IPv4, landing near €7/mo real —
roughly €84/year for a bot whose API usage costs well under $1/year.

**DDoS billing exposure is nil.** Inbound bandwidth is unbilled on all major
providers; only egress is charged. With no listening ports, no domain, and no DNS
record, there is no public service to flood and nothing that would respond at
volume. A flood makes the VM briefly unreachable; it costs nothing.

**Liveness monitoring is free and built in:** if the host dies, the next expense
message gets no confirmation reply.

**Parked: home self-hosting on a Raspberry Pi**, covering hardware selection and
running several Docker projects on one box. See `docs/backlog.md`.

**Actual outcome (2026-08-11):** Oracle Always Free was abandoned during
provisioning, not for cost or reclamation risk, but for two compounding
practical problems: the OCI console's inline VCN/subnet creation failed
repeatedly and needed the separate VCN Wizard instead, and `VM.Standard.A1.Flex`
was out of capacity in all three Frankfurt ADs with no ETA — a widely-reported,
unpredictable shortage for the free Ampere shape. finappa ended up deployed as a
second `docker compose` project on a Hetzner server already paid for and running
another app, making marginal hosting cost **€0**. This is a stronger fit for the
original requirement (structurally impossible bill), not a compromise of it — it
just wasn't the anticipated shape of the answer. The Hetzner rejection above was
about *dedicated* Hetzner pricing; co-locating on an already-owned box sidesteps
that math entirely. Full reasoning trail is in the `finappa-hosting-plan` memory
note, not duplicated here.

### 10.3 One-time setup runbook (for README)

1. Create the bot with `@BotFather`, copy the token.
2. Get your user ID (`@userinfobot`), set `ALLOWED_TELEGRAM_USER_ID`.
3. Create a **private channel** for backups, add the bot as an admin with post
   rights, and obtain its ID (forward a channel message to `@userinfobot`, or read
   it from the bot's own logs). It is negative, e.g. `-1001234567890`.
4. Fill `.env` from `.env.example`, edit `recurring.yml`.
5. `docker compose up -d --build`.

## 11. Testing

pytest against a temporary SQLite file, with the OpenAI client stubbed at the
`extractor` boundary. No network in tests.

| Test | Asserts |
|---|---|
| `test_extractor` | Validation rejects non-EUR, non-positive, and absurd amounts; empty result is handled; API failure surfaces a user-facing error. |
| `test_categorize` | A >85% fuzzy hit overrides the LLM's category; a miss creates the merchant with the LLM's category; `/recat` repoints the mapping **and** backfills past transactions for that merchant only. |
| `test_analytics` | Refunds reduce expenses; net arithmetic; **a transaction at 00:30 local on the 1st falls in the correct month** (Berlin/UTC boundary). |
| `test_recurring` | Running the check twice inserts one row; a rule whose day has not arrived does not fire. **Superseded 2026-08-11 (§8.2 addendum):** the suite no longer asserts a missed day self-heals — it now asserts the opposite (`test_day_already_passed_does_not_fire`), matching the exact-day-only behavior. |
| `test_undo` | Removes all rows of the last message batch and only those; never removes recurring rows; no-op on an empty ledger. |
| `test_backup` | `VACUUM INTO` produces a readable database; encrypt→decrypt round-trips to a byte-identical, openable database; pruning keeps exactly the newest 7. |

## 12. AI tooling deliverables

- **`AGENTS.md`** — the cross-tool standard. Architecture summary, the invariants
  list (money in cents; EUR only; UTC in the DB and Berlin for boundaries; auth is
  `filters.User`; the ledger is the recurring-idempotency state), file map, common
  tasks, deploy runbook, and an explicit **"do not add"** list: no FastAPI, no
  Caddy, no webhooks, no Alembic, no `thefuzz`, no new dependency that replaces a
  handful of stdlib lines.
- **`CLAUDE.md`** — one line deferring to AGENTS.md so the two cannot drift.
- **`.cursor/rules/finappa.mdc`** — `alwaysApply: true`, pointing at AGENTS.md and
  restating the invariants.
- **`.claude/skills/`** — `deploy`, `add-category`, `restore-backup`, `add-command`.
  `restore-backup` must cover the full path: pull the `.enc` from the channel,
  decrypt it with `BACKUP_ENCRYPTION_KEY`, verify it opens, swap it in, restart.

## 13. Security model

### 13.1 Transport

All traffic is outbound HTTPS — `api.telegram.org` and `api.openai.com`. Removing
the webhook removed the need to *terminate* TLS, not TLS itself.

The process opens **no listening sockets**. `docker-compose.yml` has no `ports:`
key and must never gain one: Docker writes its own iptables rules that **bypass
ufw**, so a published port is reachable even when the firewall is configured to
deny it. This is recorded as a hard rule in AGENTS.md.

### 13.2 What is exposed, and to whom

| Party | Sees | Mitigation |
|---|---|---|
| Telegram | Every message, in plaintext. Bot chats are **never** end-to-end encrypted — Secret Chats do not work with bots. | None possible. Accepted. |
| Telegram | The backup database dump | Fernet-encrypted before upload (§8.1) |
| OpenAI | Transaction text sent for extraction | Not trained on by default; ~30-day abuse retention. Merchant names and amounts only. |
| Anyone with the bot token | Everything the bot can see and do | `.env` gitignored; token never pasted anywhere |
| Strangers who find the bot | Nothing. `filters.User` drops their updates before any handler runs — no reply, no `/help`. | — |

The backup channel **must be private**. A public channel makes the entire
financial database a public download.

`BACKUP_ENCRYPTION_KEY` must be stored in a password manager. Losing it makes every
remote backup unrecoverable; local snapshots stay in plaintext, so a VPS that is
still alive is unaffected.

### 13.3 Host hardening (README checklist)

The VPS's SSH is the realistic attack surface, not the bot:

- `PasswordAuthentication no`, key-only auth
- `PermitRootLogin no`
- `ufw default deny incoming`, allow 22 only
- `unattended-upgrades` enabled
- `chmod 700 ./data`

### 13.4 Non-threats

Prompt injection is out of scope: one whitelisted user authors every message that
reaches the LLM.

## 14. Rejected, with reasons

| Rejected | Reason |
|---|---|
| Webhooks + Caddy + domain | Polling removes the entire ingress tier (§2.1). |
| FastAPI / Uvicorn | Nothing serves HTTP. |
| `security.py` | `filters.User` is the whitelist (§2.2). |
| Google Drive backups | Service accounts have no storage quota (§8.1). |
| Separate cron container | `JobQueue` is in-process (§8). |
| Multi-stage Dockerfile | No build step to stage (§10). |
| `thefuzz` | `difflib.get_close_matches` is one stdlib line (§6.2). |
| Regex fast-path | Second code path, saves under a cent (§6.2). |
| Alembic | One user, one database; `create_all()` suffices. |
| Float money | Integer cents (§4.1). |
| Separate recurring-runs table | The ledger is the state (§8.2). |
| Monthly recurring job | Skips a month if the container is down on the 1st (§8.2). |
| Remote backup retention | `deleteMessage` cannot reach past 48 hours (§8.1). |
| Partial logging of a mixed-validity message | Re-sending the correction would duplicate the valid items (§5.3). |
| Compensating entries for `/recat` | Zero-sum pairs pollute every future report; backups are the audit trail (§6.3). |
