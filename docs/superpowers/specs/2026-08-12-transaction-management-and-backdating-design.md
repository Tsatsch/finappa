# Transaction Management & Backdating — Design

Date: 2026-08-12
Status: Approved (user confirmed scope)
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md)

## 1. Overview

Two related features that make the ledger easier to fix without re-sending messages:

1. **Transaction management commands** — list, delete, and edit any logged
   message-sourced row by database ID.
2. **Backdating on new messages** — when the user says "yesterday I spent 30 at
   Rewe", the transaction lands on yesterday's calendar day (Berlin-local),
   not today.

Voice notes, receipt photos, and refunds remain out of scope.

## 2. Goals

- Fix a wrong amount, category, or date on any past message-sourced transaction.
- Delete a specific wrong row without walking `/undo` backward through history.
- Log purchases on the correct calendar day when the user states one in natural
  language.
- Keep recurring auto-entries protected — same invariant as `/undo`.

## 3. Non-goals

- Editing via the stats website (read-only stays read-only).
- Bulk edit across merchants (`/recat` already handles category backfill by
  merchant).
- Reply-to-message UX (v2 — needs storing Telegram message IDs).
- Arbitrary natural-language edits ("change my Rewe to 35") — structured
  commands only for edits.
- Backdating recurring rules (still fire on their configured day).

## 4. Commands

### 4.1 `/recent [n]`

- Default `n = 10`, max `50`.
- Lists the **newest** message-sourced and recurring rows (all sources), one
  line each, newest first:

  ```text
  #47 · 24,78 € · Groceries · Rewe · 2026-08-11
  #46 · 750,00 € · Housing · Rent · 2026-08-01 · recurring
  #45 · 15,00 € · Transport · Shell · 2026-08-10
  ```

- Fields: `id`, formatted amount, category, merchant/label, **local date**
  (Berlin), optional `· recurring` suffix when `source == "recurring"`.
- Empty ledger → `"No transactions yet."`

### 4.2 `/delete <id>`

- Deletes the single row with that primary key.
- **Blocked** when `source == "recurring"` — reply:
  `"#46 is a recurring entry — edit recurring.yml instead."`
- Unknown ID → `"No transaction #<id>."`
- Success → `"Deleted #47 · 24,78 € · Groceries · Rewe · 2026-08-11."`

### 4.3 `/edit <id> <field> <value...>`

Supported fields:

| Field | Example | Behavior |
|-------|---------|----------|
| `amount` | `/edit 47 amount 35.50` | Sets `amount_cents` (positive EUR float → int cents, same bounds as extractor) |
| `category` | `/edit 47 category Groceries` | Sets `category_id` on **this row only** (does not touch merchant memory or other rows — unlike `/recat`) |
| `date` | `/edit 47 date 2026-08-10` | Sets `ts` to **noon** on that local calendar day in `settings.timezone`, stored as UTC |

- **Blocked** when `source == "recurring"` (same message as delete).
- Unknown ID / invalid field / bad amount / unknown category → clear usage or
  validation error.
- Success echoes the updated row in the same format as `/recent`.

Usage line on error:

```text
Usage: /edit <id> amount <eur> | category <name> | date <YYYY-MM-DD>
```

### 4.4 `/undo` (unchanged)

Stays as the fast path for "remove everything from my last message." Complements
ID-based delete — does not overlap.

## 5. Backdating on new messages

### 5.1 Extractor change

Add optional field to `TransactionItem`:

```python
local_date: date | None = None  # calendar day in settings.timezone
```

Update the system prompt: when the user states when something happened ("yesterday",
"on Monday", "Aug 5"), set `local_date` to that calendar day; when no date is
mentioned, leave it `None`.

The LLM returns ISO dates (`YYYY-MM-DD`). Validation in `validate_extraction`:

- Reject `local_date` in the **future** (relative to Berlin-local today).
- No minimum lookback — user may backfill old months intentionally.

### 5.2 Timestamp assignment

When `local_date` is set:

```python
ts = datetime(local_date.year, local_date.month, local_date.day, 12, 0, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
```

Noon local avoids midnight UTC/month-boundary edge cases near `month_bounds_utc`.

When `local_date` is `None`, keep current behavior: `ts = datetime.now(timezone.utc)`
at message processing time.

Apply the same rule in `confirm_category` — pending confirmations store the
resolved `ts` at hold time (from the item's `local_date` or `now`).

### 5.3 Reply text

Logged lines include the local date when it differs from "today" in Berlin:

```text
• 30,00 € · Rewe · Groceries · 2026-08-11
```

When date is today, omit the date suffix (keep replies compact).

## 6. Architecture

All logic in plain functions in `app/handlers.py` (or a small `app/ledger.py`
module if handlers grows too large — prefer handlers until it hurts):

- `recent_text(session_factory, tz_name, limit) -> str`
- `delete_text(session_factory, tz_name, tx_id) -> str`
- `edit_text(session_factory, tz_name, tx_id, field, value) -> str`

Helpers:

- `format_transaction_line(session, tx, tz_name) -> str` — shared by recent/delete/edit replies.
- `local_date_for_ts(ts, tz_name) -> date`
- `ts_from_local_date(local_date, tz_name) -> datetime`

Register three new `CommandHandler`s. Extend `HELP_TEXT`.

No schema migration — `Transaction.ts` already exists; backdating only changes
how it is populated.

## 7. Invariants preserved

- Money stays `amount_cents: int`, positive; direction is `type`.
- EUR only at extractor boundary.
- Auth: `filters.User(...)` on every handler.
- Recurring rows: `source='recurring'`, `source_update_id=NULL` — unreachable by
  `/undo`, `/delete`, and `/edit`.
- All-or-nothing message validation unchanged.
- Month boundaries still via `month_bounds_utc` on `Transaction.ts`.

## 8. Error handling

| Case | Reply |
|------|-------|
| Bad `/edit` syntax | Usage line |
| Amount ≤ 0 or > MAX_AMOUNT | Same bounds message as extractor |
| Unknown category | List valid categories (like `/recat`) |
| Invalid date format | `"Use YYYY-MM-DD, e.g. 2026-08-10"` |
| Future date (edit or extract) | `"That date is in the future."` |
| Recurring row | `"#N is a recurring entry — edit recurring.yml instead."` |

## 9. Testing

| Test file | Cases |
|-----------|-------|
| `tests/test_handlers.py` | `/recent` ordering and limit; delete success; delete recurring blocked; edit amount/category/date; edit recurring blocked; backdated message lands in correct month |
| `tests/test_extractor.py` | `local_date` passthrough; future date rejected |
| `tests/test_analytics.py` | Backdated row appears in correct month's `/summary` |

## 10. Documentation updates

- `docs/backlog.md` — remove "Editing in place" and "Backdating" from deferred;
  remove voice/receipt if desired.
- `AGENTS.md` — note new commands in file map if helpful (minimal).
- `HELP_TEXT` — source of truth for user-facing command list.

## 11. Approaches considered

**Backdating**

| Approach | Verdict |
|----------|---------|
| LLM `local_date` field (chosen) | Natural UX, one code path for new messages |
| `/log 2026-08-11 ...` command | Redundant with free text |
| `/edit date` only, no LLM | Can't get day right on first log |

**Transaction management**

| Approach | Verdict |
|----------|---------|
| Separate `/recent`, `/delete`, `/edit` (chosen) | Matches existing command style; easy to test |
| Unified `/tx` subcommand | Fewer commands but awkward in Telegram |
| Reply-to bot message | Best UX later; out of scope v1 |
