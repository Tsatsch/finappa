# Reliability & Ledger UX — Design

Date: 2026-08-25
Status: Approved (user confirmed scope and design sections)
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md)

## 1. Overview

One combined package for a bot that is already in daily use:

1. **Backup health** — detect when daily snapshots have stopped landing, and
   expose `/backup-status`.
2. **Missed recurring notify** — on startup, DM when exact-day rules were
   skipped because the bot was down (still no auto-catch-up).
3. **`/summary [YYYY-MM]`** — look at a past month without opening stats/CSV.
4. **`/find <query>`** — search merchant names and notes.

Voice, receipts, home self-hosting, budgets, and second backup destinations
remain out of scope.

## 2. Goals

- Notice a missed backup within about two days without watching the channel.
- See backup configuration and newest local snapshot from Telegram.
- Notice skipped recurring entries when the process comes back, so they can be
  logged manually if desired.
- Query past-month summaries and find rows by merchant/note text.

## 3. Non-goals

- Home self-hosting / Pi migration.
- Auto-catch-up inserts for missed recurring rules (exact-day-only stays).
- Remote Telegram backup retention / pruning (API cannot delete old docs).
- Second backup destination (Drive, S3, etc.).
- Spam suppression file for repeated stale alerts across rapid restarts
  (acceptable for one user; revisit only if it becomes noisy).
- Category search in `/find`.
- Future-month summaries.

## 4. Backup health

### 4.1 Signal

Newest local plaintext snapshot matching `backup-YYYY-MM-DD.db` in the database
directory (same files `create_snapshot` / `prune_snapshots` already use). Parse
the date from the filename. No new table or marker file.

### 4.2 Stale rule

In `settings.timezone` (default `Europe/Berlin`):

- No matching snapshot → stale.
- `(today_local.date - snapshot_date).days > 2` → stale.
- Age of exactly 2 days → **not** stale (one missed night is OK).

### 4.3 When it runs

1. Once at **startup** (JobQueue `run_once(..., when=0)` with
   `job_kwargs={"misfire_grace_time": None}`).
2. **Daily at 12:00** local (same timezone helper as other jobs).

Both paths call the same plain function; if stale, DM the allowed user once per
check. Example:

```text
Backup overdue: newest local snapshot is 2026-08-20 (5 days old).
```

or, if none exist:

```text
Backup overdue: no local snapshots found.
```

The existing 03:30 backup job already DMs on exception; this covers “job never
ran” (host down, process stuck, etc.).

### 4.4 `/backup-status`

Reply includes:

- Newest snapshot date (or “none”)
- Age in Berlin calendar days (or n/a)
- OK vs overdue per the stale rule
- Whether `BACKUP_CHANNEL_ID` is set (yes/no)
- Whether `BACKUP_ENCRYPTION_KEY` is configured (yes/no) — never print the key

Register in `register_handlers`, `HELP_TEXT`, and `BOT_COMMANDS`.

### 4.5 Restore skill polish

Extend `.claude/skills/restore-backup/SKILL.md` with a short **quarterly verify**
checklist: decrypt a recent `.enc` → `PRAGMA integrity_check` → row count → stop
**without** swapping the live DB unless actually restoring.

## 5. Missed recurring (startup only)

After the existing startup recurring check (exact-day fire path unchanged), run a
**notify-only** scan:

For each **active** `RecurringRule` where:

- `day_of_month < today_local.day` (day already past this Berlin month), and
- no `Transaction` with that `recurring_rule_id` in
  `[month_start_utc, next_month_utc)` from `month_bounds_utc`

→ collect; **do not insert**.

If non-empty, one DM:

```text
Recurring skipped this month (bot was down on their day — log manually if needed):
• Rent (day 1)
• Insurance TK (day 1)
```

Not notified: rules whose day is today (normal fire path), day still ahead this
month, inactive rules, or rules that already have a ledger row this month.

## 6. `/summary [YYYY-MM]`

- No args → current Berlin month (existing behavior).
- One arg matching `YYYY-MM` → that month: pass `at` as noon on the 1st of that
  month in `settings.timezone` into `monthly_summary` / `month_bounds_utc`.
- Month after the current Berlin year-month → `"That month is in the future."`
- Bad format or extra tokens → `Usage: /summary [YYYY-MM]`
- For a non-current month, prefix the formatted body with
  `Summary for YYYY-MM:` so the reply is unambiguous.
- Update `HELP_TEXT` and `BOT_COMMANDS` description.

## 7. `/find <query>`

- Query = remainder of the message after `/find` (strip); empty → usage
  `Usage: /find <query>`.
- Case-insensitive substring match on `Merchant.display_name` **or**
  `Transaction.notes` (outer/left join merchant so note-only / recurring rows
  still match on notes).
- Order by `Transaction.id` descending, hard cap **20**.
- Format via existing `format_transaction_line` (same as `/recent`).
- No hits → `No matches for '<query>'.`
- Register handler, `HELP_TEXT`, and `BOT_COMMANDS`.

## 8. Invariants preserved

- Money stays `amount_cents: int`, positive; direction is `type`.
- EUR only at extractor boundary.
- `Transaction.ts` UTC; month math via `month_bounds_utc`.
- Auth: `filters.User(...)` on every new command handler.
- Recurring: exact-day fire only; ledger remains idempotency state; missed
  notify never inserts.
- No `ports:` on the bot; no new HTTP surface; no Alembic; no new dependencies.
- Every new JobQueue job uses `misfire_grace_time=None`.

## 9. File touch map (expected)

| Area | Likely files |
|------|----------------|
| Backup status / stale math | `app/backup.py`, `app/main.py`, `app/handlers.py` |
| Missed recurring | `app/recurring.py`, `app/main.py` |
| Summary month arg | `app/handlers.py`, possibly tiny helper in `app/analytics.py` |
| Find | `app/ledger.py`, `app/handlers.py` |
| Menu / help | `BOT_COMMANDS`, `HELP_TEXT`, add-command skill if wording drifts |
| Docs | restore-backup skill; `docs/backlog.md` note that this package shipped |
| Tests | `tests/test_backup.py`, `tests/test_recurring.py`, `tests/test_handlers.py`, `tests/test_main.py` as needed |

## 10. Testing

| Case | Expectation |
|------|-------------|
| Snapshot age ≤ 2 days | not stale |
| Snapshot age > 2 days / missing | stale; DM text |
| `/backup-status` | date, age, OK/overdue, channel/key flags |
| Missed rule past day, no row | listed in startup DM; no insert |
| Rule already logged / day today / day ahead | not listed |
| `/summary` no args | current month |
| `/summary 2026-07` (past) | prefixed summary for that month |
| `/summary` future | future rejection |
| `/summary` bad | usage |
| `/find Rewe` | merchant hits, newest first, ≤ 20 |
| `/find` notes / case | matches |
| `/find` empty / no hits | usage / no-matches text |

## 11. Documentation updates

- This spec.
- `.claude/skills/restore-backup/SKILL.md` — quarterly verify section.
- `HELP_TEXT` / `BOT_COMMANDS` — `/backup-status`, `/find`, `/summary` wording.
- `docs/backlog.md` — mark this package as shipped when implemented (or remove
  stale “transaction management still pending” wording if still present).
