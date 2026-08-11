# finappa — Developer Overview

A guided tour of how the code actually works, for a developer picking this up
for the first time. For *why* things are built this way, see the design specs
in `docs/superpowers/specs/`; for the terse rule list, see `AGENTS.md`; for
deployment, see `README.md`. This document is the middle layer: what happens,
in what order, across which files.

## The shape of the system

One Python process. No web server, no separate database service, no message
queue. `python-telegram-bot` long-polls Telegram for updates; SQLAlchemy talks
to a single SQLite file in WAL mode; two background jobs run on the same
process's `JobQueue`. That's the whole runtime.

```
Telegram  ──long poll──>  app/main.py (Application)
                               │
                               ├── app/handlers.py   (message + command + button handlers)
                               ├── JobQueue: recurring_job   (app/main.py, daily 06:00 + once at startup)
                               └── JobQueue: backup_job      (app/main.py, daily 03:30)
                               │
                               ▼
                          app/db.py (SQLite, WAL)
```

Nothing in this system listens on a network port. `docker-compose.yml` has no
`ports:` key. The bot reaches out to `api.telegram.org` and `api.openai.com`;
nothing reaches in.

## The message lifecycle

This is the core flow — a text message becomes zero or more ledger rows (or a
held confirmation prompt). Walk it top to bottom:

**1. A text message arrives.** `register_handlers` (`app/handlers.py`) wires a
single `MessageHandler` gated by `filters.User(user_id=allowed_user_id) &
filters.UpdateType.MESSAGE` — the `UpdateType.MESSAGE` half excludes edited
messages specifically, since PTB would otherwise route them here with
`update.message` set to `None`. `handle_message` runs `process_message` inside
`asyncio.to_thread(...)`, because it does a blocking OpenAI HTTP call and
blocking SQLite work, and this event loop also needs to keep servicing other
commands and the two background jobs while that's in flight.

**2. Extraction.** `process_message` (`app/handlers.py`) calls
`extract_transactions` (`app/extractor.py`), which sends the raw text plus a
system prompt to `gpt-4o-mini` via OpenAI Structured Outputs, constrained to
the `ExtractionResult` schema (a list of `TransactionItem`). Every item carries
`amount`, `currency`, `merchant`, `category` (one of the 14 `CategoryName`
values, closed enum — the model cannot invent a category), `category_confident`
(bool, no default — strict-mode schemas forbid defaults), `type`
(`EXPENSE`/`INCOME`/`REFUND`), and optional `notes`.

**3. Validation — all or nothing.** `validate_extraction` rejects the *entire*
message if any item has a non-EUR currency or an amount outside
`(0, 1_000_000]`. Not per-item: the whole message. Reasoning: if one item of
three gets silently dropped, the user's natural next move is to retype the
message, which would double-log the two that succeeded. All-or-nothing makes
"just resend it" always correct. Any OpenAI-side failure (timeout, refusal,
network error) is also wrapped into the same `ExtractionError`, so
`process_message` has exactly one exception type to catch.

**4. Per-item routing.** Back in `process_message`, each validated item takes
one of three paths:

- **Known merchant** (`find_matching_merchant` in `app/categorize.py` finds a
  fuzzy match ≥ 0.85 via stdlib `difflib`) → the merchant's *stored* category
  wins, unconditionally. The LLM's guess this round is discarded even if
  `category_confident` was true. This is what keeps "Rewe" landing in
  Groceries forever instead of drifting.
- **New/no merchant, `category_confident: true`** → logged immediately.
  `resolve_category` creates the `Merchant` row using the LLM's category,
  which becomes that merchant's stored category from now on.
- **New/no merchant, `category_confident: false`** → **held, not logged.** A
  `PendingConfirmation` (in-memory dataclass: `pending_id`, `ts`,
  `amount_cents`, `type`, `merchant`, `notes`) is appended to a list.
  `resolve_category` is never called for this item — no `Transaction`, no
  `Merchant` row, nothing written.

**5. The reply.** Confidently-logged items are collected into
`ProcessResult.reply_text` (`"Logged N: ...\nNet this month: ..."`) — `None`
if every item in the message was held. `ProcessResult.pending` carries the
held items back to `handle_message`, which stores each one into
`context.bot_data["pending_confirmations"][pending_id]` and sends a separate
Telegram message per held item with an inline keyboard (14 category buttons
+ "Don't log this", built by `_category_keyboard`).

## The confirmation flow

Tapping a button sends a `CallbackQuery` with `callback_data` of the form
`pconf:{pending_id}:{category_or_SKIP}`. `handle_category_confirmation`
(registered via `CallbackQueryHandler(..., pattern=r"^pconf:")`) handles it:

1. **Auth first, always.** `CallbackQueryHandler` does not compose with
   `filters.User` the way `CommandHandler`/`MessageHandler` do, so this
   handler checks `query.from_user.id` manually as its literal first line,
   before even `await query.answer()`. This is the one handler in the
   codebase with hand-rolled auth instead of a filter — see the "Invariants"
   section of `AGENTS.md` before touching it.
2. `pending_confirmations.pop(pending_id, None)` — if it's gone (container
   restarted since the prompt was sent; this state is deliberately in-memory
   only, see below), the user gets "this expired, resend it."
3. **SKIP** → reply "discarded," nothing is written. This is the whole
   "or redact it" half of the feature — no undo needed because nothing was
   ever logged.
4. **A category** → `confirm_category` runs (also off the event loop, same
   `asyncio.to_thread` reasoning as step 1). It calls `resolve_category` with
   the *tapped* category — but if a `Merchant` row for that name now exists
   (e.g. a confident item earlier in the same message created it, or a
   second pending item for the same new merchant got confirmed first),
   `resolve_category` returns the *already-stored* category instead, same as
   any other lookup. The reply text is built from `category.name` (what was
   actually written), not the raw tapped string — a real bug caught in final
   review: showing the tapped choice would sometimes lie about what got
   saved.

The confirmed `Transaction` gets `ts=pending.ts` (the *original* message's
arrival time, not "now" — confirmation can happen minutes or hours later) and
`source_update_id=update.update_id` from the *callback's own* update, not the
original message's. That's deliberate: it gives the confirmed row its own
independent `/undo` batch, rather than entangling it with whatever else the
original message logged.

**Why in-memory, not a database table:** the failure mode of losing this state
on a restart is "resend one sentence" — nothing was ever written to the
ledger, so there's nothing to lose. A table would buy durability an
essentially-free-to-retry action doesn't need. See `AGENTS.md`'s "Do not add"
list.

## Commands

All in `app/handlers.py`, each following the same shape: a plain function
(`session_factory` in, string or bytes out — no PTB types, directly unit
testable) plus a thin `async def handle_*` wrapper that reads from
`context.bot_data`, calls the plain function, and replies.

| Command | Plain function | What it does |
|---|---|---|
| `/summary` | `summary_text` | `monthly_summary` aggregates the current month (Berlin-local boundaries, converted to UTC) into income/expenses/net/by-category. |
| `/undo` | `undo_last` | Finds the most recent `Transaction` with a non-null `source_update_id`, deletes every row sharing that id. Rows from recurring jobs have `source_update_id = NULL` and are structurally unreachable here — `/undo` can never touch them. |
| `/export` | `export_csv` | Streams every transaction as CSV, built with the stdlib `csv` module into an in-memory buffer. |
| `/recat <merchant> <category>` | `recat_text` → `recat_merchant` (`app/categorize.py`) | Fuzzy-matches the merchant (same `find_matching_merchant`, same 0.85 cutoff as the write path — so it can find whatever name the LLM actually stored), repoints its category, and bulk-`UPDATE`s every past `Transaction` for that merchant in one statement. Not a compensating reversal-entry pair — that would leave zero-sum rows polluting every future `/summary`. |
| `/help`, `/start` | — | Static help text. |

## The two background jobs

Both scheduled in `build_application` (`app/main.py`) on PTB's `JobQueue` —
no cron, no separate container, no HTTP trigger endpoint.

**`recurring_job`** — daily at 06:00 Europe/Berlin, plus once at process
startup (`run_once(..., when=0)`). All three `JobQueue` jobs (this one, the
daily version, and `backup_job`) are registered with
`job_kwargs={"misfire_grace_time": None}`: APScheduler's default grace window
is 1 second, and a `when=0` job targets "right now," so any startup delay
past 1 second (normal — connecting to Telegram, etc.) caused the job to be
*silently skipped entirely* rather than run late. Confirmed live in
production — a startup run missed by 2.5 seconds and never executed, so a
recurring transaction never got logged. Delegates to `run_recurring_check`
(`app/recurring.py`), which is the whole idempotency mechanism in one query:
for each active `RecurringRule` whose `day_of_month` **exactly equals**
today's local day, check whether a `Transaction` with that `recurring_rule_id`
already exists in the current month's window; if not, insert one. **The
ledger itself is the state** — there is no separate "has this run" table to
drift out of sync, so running the check twice on the same matching day is
always safe (the second run sees the row and no-ops).

Note the exact match, not `<=`: **a rule does not catch up if its day already
passed this month** — it waits for next month. This was a deliberate reversal
(2026-08-11) of the original design, which self-healed across downtime by
firing on any later day within the month. That surfaced during initial
deployment — a rule with `day_of_month: 1` fired on day 11 (the day the bot
was actually set up), which is exactly what self-healing was designed to do,
but reads as a surprising backdated-feeling entry in practice. Trade-off
accepted knowingly: if the container is down specifically on a rule's day,
that month's entry is skipped entirely, not caught up later. See
`app/recurring.py`'s inline comment and the design spec's §8.2 addendum for
the full reasoning trail.

`recurring.yml` is loaded into `RecurringRule` rows at every startup by
`load_recurring_rules` (upsert by `key`; validates `day_of_month` is 1–28 and
`type` is one of the three valid values, raising loudly at startup rather
than silently accepting garbage).

**`backup_job`** — daily at 03:30 Europe/Berlin. `create_snapshot`
(`app/backup.py`) runs `VACUUM INTO` (never a plain file copy — the live DB is
in WAL mode, and copying the main file without the `-wal`/`-shm` companions
mid-write can produce a corrupt snapshot). If `BACKUP_CHANNEL_ID` is set, the
snapshot is Fernet-encrypted (`encrypt_file`) and sent to that Telegram
channel; the encrypted temp file is deleted immediately after regardless of
success. `prune_snapshots` then trims local snapshots to the newest 7 — inside
a `finally`, so a failed remote send still prunes and still notifies (a
`try/except` around the whole body DMs "Backup failed: ..." on any exception).

## Data model

Four tables, `create_all()` at startup, no migration framework (one user, one
database — see `AGENTS.md`).

```
Category          id, name (unique) — seeded from CategoryName enum
Merchant          id, name (unique, normalized), display_name, category_id → Category
Transaction       id, ts (UTC), amount_cents (int, always positive), type,
                  merchant_id → Merchant (nullable), category_id → Category,
                  notes, source ("message" | "recurring"),
                  source_update_id (nullable, indexed), recurring_rule_id → RecurringRule (nullable)
RecurringRule     id, key (unique), label, amount_cents, type, category_id → Category,
                  day_of_month (1–28), active
```

Money is `amount_cents: int`, always positive — direction lives entirely in
`type`, never in the sign. The only float in the system is
`TransactionItem.amount` at the LLM boundary, converted immediately with
`round(amount * 100)`. `Transaction.ts` is UTC; every "current month"
computation goes through `month_bounds_utc` (`app/analytics.py`), which
converts the configured local timezone's month boundary to UTC before
querying — getting this backwards is exactly the kind of bug that only shows
up once a year, near midnight, on the 1st.

`make_engine` (`app/db.py`) enables both `PRAGMA journal_mode=WAL` and
`PRAGMA foreign_keys=ON` on every new connection via a SQLAlchemy `connect`
event — SQLite disables FK enforcement by default per-connection, so this is
what actually makes the `ForeignKey` declarations above real constraints
rather than decoration.

## File map

| File | Owns |
|---|---|
| `app/config.py` | `Settings` (pydantic-settings) — every env var, one cross-field validator (backup key required iff backup channel set) |
| `app/categories.py` | `CategoryName` — the single source of truth for valid categories, both seeds the DB and constrains the LLM schema |
| `app/db.py` | SQLAlchemy models, WAL+FK engine setup, idempotent category seeding |
| `app/extractor.py` | OpenAI call, extraction schema, all-or-nothing validation |
| `app/categorize.py` | Fuzzy merchant matching (`find_matching_merchant`), merchant/category resolution, `/recat` |
| `app/analytics.py` | Timezone-correct month boundaries, money formatting, `/summary` math |
| `app/recurring.py` | `recurring.yml` loading, the idempotent daily check |
| `app/backup.py` | Snapshotting, encryption, pruning — no Telegram/PTB imports, pure file operations |
| `app/handlers.py` | Every Telegram-facing behavior: the message pipeline, the confirmation flow, all commands |
| `app/main.py` | Wires it all together, owns the two job schedules and the error handler, process entrypoint |

## Security model, briefly

- **Auth:** `filters.User(user_id=...)` on every message/command handler; a
  manual `from_user.id` check on the one `CallbackQueryHandler` (filters don't
  compose with it). No `security.py`, no other auth path.
- **No inbound exposure:** long polling means no webhook, no listening socket,
  no port ever published in `docker-compose.yml`.
- **Backups are encrypted before they leave the host** (Fernet), because the
  destination is a Telegram channel — Telegram sees plaintext for every live
  message already (bots can't use Secret Chats), so the backup file is the one
  place worth encrypting independently.
- **finappa can safely share a host with other Docker projects** — it never
  publishes a port, so there's nothing for it to conflict over.

Full threat model and reasoning: `docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md` §13.

## Testing

Every module with real logic has tests against a real on-disk SQLite file
(`tmp_path` fixture — WAL mode needs a real file, `:memory:` won't exercise
it) and a `MagicMock`-stubbed OpenAI client. PTB-layer async handlers that
have real branching logic (`recurring_job`, `backup_job`,
`handle_category_confirmation`) are tested directly using
`SimpleNamespace(bot_data=..., bot=AsyncMock())` fakes rather than mocking PTB
itself. Thin one-line wrappers (`handle_summary`, `handle_undo`, etc.) aren't
tested separately — their entire logic is "call the plain function, reply
with the result," already covered by testing the plain function.

```bash
pytest -v   # no network, no real Telegram/OpenAI calls anywhere
```

## Where to go next

- **Adding a category:** `.claude/skills/add-category/SKILL.md`
- **Adding a command:** `.claude/skills/add-command/SKILL.md`
- **Deploying:** `.claude/skills/deploy/SKILL.md`, `README.md`
- **Restoring a backup:** `.claude/skills/restore-backup/SKILL.md`
- **Full design rationale (the "why" behind every choice above):**
  `docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md` and
  `docs/superpowers/specs/2026-08-11-category-confirmation-design.md`
- **Planned/future work:** `docs/backlog.md`
