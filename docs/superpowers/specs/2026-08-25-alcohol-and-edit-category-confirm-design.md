# Alcohol Category & Edit-Category Confirm — Design

Date: 2026-08-25
Status: Approved
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md),
[2026-08-12-transaction-management-and-backdating-design.md](2026-08-12-transaction-management-and-backdating-design.md)

## 1. Overview

Two small, related quality improvements:

1. **Alcohol** category for beer/kiosk/going-out spend.
2. **`/edit … category` confirmation** when the row has a merchant — choose
   row-only vs merchant memory + history backfill (like `/recat`), or cancel.

Merchant aliases and LLM-assisted cleanup remain out of scope (later brainstorm).

## 2. Alcohol category

Add `ALCOHOL = "Alcohol"` to `CategoryName` in `app/categories.py`.
`init_db` seeds it on startup. No migration. Restart required so the LLM schema
and confirmation keyboards include it.

## 3. `/edit category` with merchant

### 3.1 When buttons appear

Only for `/edit <id> category <CategoryName>` when:

- Transaction exists and is not recurring (existing blocks apply), and
- `merchant_id` is not null.

Otherwise category (and amount/date) behave as today — immediate write.

### 3.2 Hold, don’t write

Validated edits are stored in
`context.bot_data["pending_edits"][token]` with at least:
`tx_id`, `category_name`, `merchant_id`, `pending_id`.

Reply text summarizes the change; keyboard:

| Button | Action |
|--------|--------|
| This row only | Set `category_id` on this transaction only |
| Merchant + all past | Set merchant’s `category_id` and update all txs for that merchant (same as `/recat`) |
| Cancel | Discard pending; no writes |

Callback data: `pedit:{token}:{ROW|MERCHANT|CANCEL}`.

### 3.3 Auth & expiry

`CallbackQueryHandler` does not compose with `filters.User` — check
`query.from_user.id` against `allowed_telegram_user_id` as the first line
(same as category confirmation). Missing token after restart → expired message,
ask to re-run `/edit`.

### 3.4 Success copy

Echo the updated row line (existing format). For MERCHANT, also note how many
past transactions were updated.

## 4. Invariants

- Money / EUR / UTC / User filter unchanged.
- In-memory pending only (no DB table) — restart costs a resend, never data.
- No new dependencies; no Alembic; no bot ports.
