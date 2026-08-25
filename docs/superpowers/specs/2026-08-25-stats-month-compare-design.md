# Stats Month Compare — Design

Date: 2026-08-25
Status: Approved (user confirmed scope and design sections)
Amends: [2026-08-11-stats-website-design.md](2026-08-11-stats-website-design.md)

## 1. Overview

Add a **Compare months** section to the read-only stats one-pager so two
calendar months can be viewed side by side (KPIs + expense category deltas).
Client-side only — same `/api/data` payload; no new routes, no bot changes.

Budgets, savings-rate widgets, and other stats ideas remain out of scope.

## 2. Goals

- Pick any two months present in the ledger and see Income / Expenses / Net
  for each.
- See which expense categories drove the difference (Δ = B − A).
- Leave existing top filters and “This period” / Trends / Merchant overview
  behavior unchanged.

## 3. Non-goals

- Server-side compare API.
- Income categories in the delta table.
- Replacing or merging with the top year/month filters.
- Charts inside the compare section (table + KPIs only).
- Deep links / URL query params for selected months (nice-to-have later).

## 4. Placement & layout

Section sits **below Trends**, **above Merchant overview**.

**Layout B (chosen):**

1. Section title + one-line lede (“Side-by-side totals and expense category changes”).
2. Two columns:
   - Left: Month A `<select>` + Income / Expenses / Net for A
   - Right: Month B `<select>` + Income / Expenses / Net for B
3. Full-width table below:
   - Columns: Category | A (€) | B (€) | Δ (€)
   - Expenses only
   - Sorted by absolute Δ descending
   - Omit rows where both A and B expense totals for that category are 0
   - If no rows remain: short empty copy, e.g. `No expense differences.`

Reuse existing stats typography / table styles where practical (`data-table`,
KPI patterns). Prefer matching the current page’s EUR formatting helper in
`app.js` rather than inventing a second format.

## 5. Month pickers

- Options: every distinct `(local_year, local_month)` present in
  `transactions`, labeled `YYYY-MM` (or `Mon YYYY` if that matches nearby UI —
  prefer `YYYY-MM` for unambiguous sorting).
- **Defaults:** Month A = previous calendar month relative to “today” in the
  ledger timezone; Month B = current calendar month. If a default month has no
  transactions, fall back to the nearest available month in the option list
  (prefer earlier for A, later for B when possible; if only one month exists,
  both pickers may show that month).
- Changing either picker recomputes KPIs + table from the in-memory ledger
  (no refetch).

## 6. Math

- Filter transactions where `local_year` / `local_month` match the selected
  month (same fields already on each row from `fetch_ledger`).
- KPIs: same rules as existing `summarize()` — sum INCOME, sum EXPENSE, net =
  income − expenses (amounts always positive cents; type carries direction).
- Category Δ: for EXPENSE rows only, sum `amount_cents` by `category` per
  month; Δ_cents = B − A.
- Display Δ with an explicit sign; zero as `0,00 €` (or the page’s existing
  zero format).

## 7. Independence from top filters

Top year / month / category / type filters continue to drive only “This period”
charts and KPIs. Compare months **ignores** those filters so a filtered period
view and a two-month compare can coexist without fighting.

Merchant overview keeps its own filter strip (unchanged).

## 8. Invariants preserved

- Stats service remains read-only; bot still has no `ports:`.
- Money stays `amount_cents`; EUR display only; Berlin-local calendar months via
  existing `local_year` / `local_month`.
- No new dependencies (Chart.js already loaded; not required for this section).
- No Alembic / schema changes.

## 9. File touch map (expected)

| File | Change |
|------|--------|
| `stats/static/index.html` | Compare section markup |
| `stats/static/app.js` | Month options, defaults, summarize-by-month, render compare |
| `stats/static/styles.css` | Two-column compare layout, table alignment |
| `tests/test_stats.py` | Only if any server contract changes (expect none); optional JS is not unit-tested today — keep that pattern unless a tiny pure helper is extracted |

## 10. Testing

Manual after deploy:

- Defaults land on previous vs current when data exists.
- Swapping A/B updates KPIs and Δ signs.
- Category with spend only in B shows A = 0 and positive Δ.
- Empty ledger / single month does not throw; pickers still usable.

Automated: no `/api/data` shape change — existing `tests/test_stats.py` stay
green. If a pure helper (e.g. `buildMonthKeys`, `categoryExpenseDeltas`) is
extracted to a tiny shared module, add focused tests; otherwise follow the
current “logic lives in `app.js`” pattern.

## 11. Documentation

- This spec.
- `docs/backlog.md` — note month compare shipped when implemented.
