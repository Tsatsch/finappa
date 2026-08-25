# Stats Month Compare Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a client-side Compare months section (layout B: two columns + expense category Δ table) below Trends on the stats one-pager.

**Architecture:** Reuse `/api/data` and existing `summarize` / `formatCents` / `monthKey` helpers in `stats/static/app.js`. No backend changes. Compare ignores top-period filters.

**Tech Stack:** Vanilla JS, existing stats CSS patterns, Starlette static files (unchanged).

## Global Constraints

- Client-only; no new API routes or dependencies.
- Δ = B − A; category table is EXPENSE only.
- Defaults: previous calendar month vs current; fall back to nearest keys in ledger.
- Layout B: two columns with pickers + KPIs; table underneath.
- Top filters and merchant overview unchanged.
- Money display via existing `formatCents`.

## File map

| File | Role |
|------|------|
| `stats/static/index.html` | Compare section markup |
| `stats/static/app.js` | Month keys, defaults, render |
| `stats/static/styles.css` | Two-column compare layout |
| `docs/backlog.md` | Mark shipped |
| `tests/test_stats.py` | Expect still green (no API change) |

---

### Task 1: Markup + CSS for Compare section

**Files:**
- Modify: `stats/static/index.html`
- Modify: `stats/static/styles.css`

- [ ] **Step 1:** Insert section between Trends and Merchant overview:

```html
  <section class="compare-months" aria-label="Compare months">
    <h2 class="section-title">Compare months</h2>
    <p class="section-lede">Side-by-side totals and expense category changes.</p>
    <div class="compare-columns">
      <div class="compare-col">
        <label>
          Month A
          <select id="compare-month-a"></select>
        </label>
        <dl class="compare-kpis">
          <div><dt>Income</dt><dd id="compare-a-income">—</dd></div>
          <div><dt>Expenses</dt><dd id="compare-a-expenses">—</dd></div>
          <div><dt>Net</dt><dd id="compare-a-net">—</dd></div>
        </dl>
      </div>
      <div class="compare-col">
        <label>
          Month B
          <select id="compare-month-b"></select>
        </label>
        <dl class="compare-kpis">
          <div><dt>Income</dt><dd id="compare-b-income">—</dd></div>
          <div><dt>Expenses</dt><dd id="compare-b-expenses">—</dd></div>
          <div><dt>Net</dt><dd id="compare-b-net">—</dd></div>
        </dl>
      </div>
    </div>
    <div class="table-scroll">
      <table class="data-table" id="compare-table">
        <thead>
          <tr>
            <th scope="col" class="th-left">Category</th>
            <th scope="col" class="th-right" id="compare-th-a">A</th>
            <th scope="col" class="th-right" id="compare-th-b">B</th>
            <th scope="col" class="th-right">Δ</th>
          </tr>
        </thead>
        <tbody id="compare-table-body"></tbody>
      </table>
    </div>
  </section>
```

- [ ] **Step 2:** Add CSS — `.compare-columns` as 2-col grid; stack on narrow screens; `.compare-kpis` compact dt/dd; reuse filter select look inside `.compare-col label`.

- [ ] **Step 3:** Commit markup/CSS if splitting commits; else continue to Task 2 in same commit.

---

### Task 2: Compare logic in `app.js`

**Files:**
- Modify: `stats/static/app.js`
- Modify: `docs/backlog.md`

**Helpers to add:**

```javascript
function availableMonthKeys(transactions) {
  return uniqueSorted(transactions.map(monthKey));
}

function txsForMonthKey(transactions, key) {
  return transactions.filter((t) => monthKey(t) === key);
}

function defaultCompareKeys(keys, now = new Date()) {
  // keys: sorted "YYYY-MM"
  const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const prevDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const prev = `${prevDate.getFullYear()}-${String(prevDate.getMonth() + 1).padStart(2, "0")}`;
  if (keys.length === 0) return { a: "", b: "" };
  const pick = (want, preferEarlier) => {
    if (keys.includes(want)) return want;
    if (preferEarlier) {
      const earlier = keys.filter((k) => k < want);
      return earlier.length ? earlier[earlier.length - 1] : keys[0];
    }
    const later = keys.filter((k) => k > want);
    return later.length ? later[0] : keys[keys.length - 1];
  };
  return { a: pick(prev, true), b: pick(cur, false) };
}

function categoryExpenseDeltas(byCatA, byCatB) {
  const names = uniqueSorted([...Object.keys(byCatA), ...Object.keys(byCatB)]);
  return names
    .map((name) => {
      const a = byCatA[name] || 0;
      const b = byCatB[name] || 0;
      return { name, a, b, delta: b - a };
    })
    .filter((row) => row.a !== 0 || row.b !== 0)
    .sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta));
}
```

- [ ] **Step 1:** `populateCompareSelects(transactions)` — fill both selects with `YYYY-MM` options (no ALL); set defaults via `defaultCompareKeys`.

- [ ] **Step 2:** `renderCompare()` — read select values; summarize each month; set KPI text; set header labels to month keys; fill table body; empty → one row `No expense differences.`

- [ ] **Step 3:** Wire change listeners on `compare-month-a` / `compare-month-b`; call from `boot` after populate; call `renderCompare()` from `render()`.

- [ ] **Step 4:** Update `docs/backlog.md` shipped list.

- [ ] **Step 5:** `pytest tests/test_stats.py -v` — expect PASS.

- [ ] **Step 6:** Commit + push with message describing compare section.
