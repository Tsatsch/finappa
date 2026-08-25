# Merchant Aliases & `/cleanup` — Design

Date: 2026-08-25
Status: Draft (awaiting user review)
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md)

## 1. Overview

Variant/brand merchant duplicates (e.g. "Rewe City" vs "Rewe") fragment
`/summary` and the stats Merchants tab. This design adds:

1. **Durable aliases** so future logs of a variant attach to the canonical merchant.
2. **Manual `/cleanup`** — a heavier model proposes merges; the user Apply / Skip /
   Done each proposal. Nothing merges without an Apply.
3. **Session undo** for the last cleanup run, plus **rejection memory** so Skips
   are not re-suggested.

**Not in scope:** peer-chain merges (Rewe ↔ Edeka), scheduled auto-cleanup,
showing alias strings as separate merchants in UI, renaming the canonical from
cleanup, `/aliases` list (optional later).

## 2. Goals & non-goals

**Goals**

- One canonical merchant name everywhere after Apply (Telegram + stats).
- Prevent re-accumulation: aliases on the logging path; no re-proposal of
  absorbed or rejected pairs.
- Keep cost bounded: everyday traffic on a cheap model; cleanup rare and gated.

**Non-goals**

- Collapsing different supermarket chains into one merchant (category already
  aggregates Groceries).
- Rewriting historical transaction `category_id` values on merge.
- Perfect LLM judgment — the human gate is the authority.

## 3. Models & config

| Role | Env | Default | Reasoning |
|------|-----|---------|-----------|
| Logging (extract + categorize) | `OPENAI_MODEL` | `gpt-5.6-luna` | `OPENAI_REASONING_EFFORT=none` |
| `/cleanup` proposals | `OPENAI_MODEL_HEAVY` | `gpt-5.6-terra` | `OPENAI_REASONING_EFFORT_HEAVY=medium` |

`.env.example` documents both with short comments. `Settings` gains
`openai_model_heavy`, `openai_reasoning_effort`, `openai_reasoning_effort_heavy`
(defaults as above). Heavy fields are unused except by cleanup.

Logging keeps the existing Structured Outputs path; pass reasoning effort when
the SDK accepts it (Luna: `none`). Cleanup uses the same parse style with the
heavy model + `medium`.

## 4. Data model

### 4.1 `merchant_aliases`

| Column | Notes |
|--------|--------|
| `id` | PK |
| `alias_normalized` | unique; same normalize as `Merchant.name` |
| `merchant_id` | FK → canonical `merchants.id` |
| `created_at` | UTC |

### 4.2 `cleanup_rejections`

| Column | Notes |
|--------|--------|
| `id` | PK |
| `left_normalized` | |
| `right_normalized` | |
| `created_at` | UTC |

Store each unordered pair with `left_normalized <= right_normalized` so A↔B is
one row. Unique on `(left_normalized, right_normalized)`.

### 4.3 `cleanup_sessions` + `cleanup_session_merges`

**Session:** `id`, `started_at`, `undone_at` (nullable).

**Merge row** (enough to reverse one Apply):

- `session_id`, `canonical_merchant_id`
- `absorbed_normalized`, `absorbed_display_name`, `absorbed_category_id`
- `tx_ids` moved (JSON list of ints) — or equivalent reversible record
- `alias_normalized` created

Undo reverses the last session where `undone_at IS NULL`: recreate absorbed
merchant, move listed txs back, delete the alias created by that merge, set
`undone_at`. Rejections from that session **stay** (Skip still means “don’t
ask again”).

No Alembic — `create_all()` on startup (same as today).

## 5. Logging path (alias resolution)

Order in merchant resolution (`resolve_category` / `find_matching_merchant`):

1. Exact match on `Merchant.name`
2. Hit on `merchant_aliases.alias_normalized` → canonical merchant
3. Existing fuzzy (`difflib` ≥ 0.85) on merchant names
4. Create new merchant

Display always uses canonical `display_name`. Aliases are match-only; they never
appear as their own merchant in `/summary`, `/recent`, `/find`, or stats.

## 6. `/cleanup` UX

1. Load current merchants (display, normalized, category, tx count) for the
   prompt.
2. Call heavy model → Structured Outputs
   `proposals: [{from_name, to_name, reason}]` (empty list allowed). Cap ~20
   proposals per run.
3. **Server-side filters before showing any button** (do not trust the model):
   - Both names resolve to **existing** merchants.
   - **Same `category_id`** on both merchant rows — drop otherwise.
   - Pair not already aliased / not the same merchant.
   - Pair not in `cleanup_rejections`.
   - Variant/brand only is prompted; peer chains are instructed against; the
     same-category guard is the hard filter for “different kind of spend.”
4. If nothing left → “Nothing to clean up.”
5. Else open a `cleanup_sessions` row; present proposals one-by-one with
   **Apply / Skip / Done**.
6. **Apply** → merge (see §7) + session merge log. **Skip** → rejection row.
   **Done** → end session; remaining proposals are **not** rejected.
7. In-memory pointer for the active Telegram keyboard (like category confirm /
   `pedit`). Stale callbacks after Done/restart → ignore / ask to re-run
   `/cleanup`. Auth: check `query.from_user.id` first on the callback handler.

**Commands:** `/cleanup` and `/cleanup_undo` (Telegram menu names use
underscores). Register both in `BOT_COMMANDS`.

**Apply success copy (so the user sees the merge worked):**  
`Merged {from} → {to} (N transactions).`  
End-of-session: counts of Applied / Skipped.

## 7. Merge on Apply

1. Resolve `from` → absorbed, `to` → canonical (by normalized name / display).
2. Re-check same category; if not, refuse that Apply (stale/race).
3. Update all txs with `merchant_id = absorbed.id` → `canonical.id`.
   **Do not** change those rows’ `category_id`.
4. Retarget any aliases pointing at absorbed → canonical; insert alias for
   absorbed `name` → canonical.
5. Delete absorbed merchant row (FKs cleared by steps above).
6. Canonical merchant row unchanged (name, display_name, category_id).
7. Append `cleanup_session_merges` for undo.

## 8. Cost & non-repetition invariant

Applied cleanups are **not** replayed next month:

- Absorbed merchant rows no longer exist → cannot be proposed.
- Alias on the logging path stops new duplicate merchants for the same string.
- Rejected pairs stay out of prompts.

Each `/cleanup` pays for one heavy call over the **current** merchant list (minus
filters), not over cleanup history. New spellings that miss the alias can appear
as new debt — that is intentional, not a replay of old Applies.

## 9. Robustness (override surface)

| Action | Overrides? |
|--------|------------|
| Apply | Merchant identity of absorbed → canonical display everywhere; future alias string → canonical |
| Apply | **Does not** rewrite historical tx `category_id` |
| Apply | **Does not** change amounts, dates, notes, type |
| Skip / Done | No ledger merge |
| Same-category guard | Drops cross-category proposals before UI |
| Human Apply | Sole authority to merge; model only suggests |
| `/cleanup_undo` | Reverses last session’s Applies only |

Peer chains (Rewe ↔ Edeka) remain separate merchants; Groceries (or other shared
categories) already aggregate them in category totals without merchant merge.

## 10. Files (sketch)

| Area | Touch |
|------|--------|
| `app/db.py` | New tables |
| `app/config.py`, `.env.example` | Model + reasoning env vars |
| `app/categorize.py` | Alias step in resolution |
| `app/cleanup.py` | Propose, filter, merge, undo, reject (new) |
| `app/handlers.py`, `app/main.py` | Commands + callbacks + `BOT_COMMANDS` |
| Tests | Merge, alias resolve, same-category filter, rejection, undo |

No new dependencies; no Alembic; no bot `ports:`.

## 11. Invariants

- Money is `amount_cents`; EUR only; `Transaction.ts` UTC; auth via
  `filters.User` / manual callback user id check.
- SQLite remains source of truth (aliases, rejections, session log).
- No auto-merge; no peer-chain merges in product intent.
- Same-category hard filter on proposals and on Apply.
- Merge moves `merchant_id` only — never bulk-rewrite tx categories.
- Display = canonical `display_name` only (Telegram + stats).
