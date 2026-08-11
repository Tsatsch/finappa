# Read-only Stats Website — Design

Date: 2026-08-11
Status: Approved
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md)

## 1. Overview

A public, password-protected, read-only one-pager that shows income/expense
summaries and charts for personal analysis. No domain purchase — access via
`http://<host-ip>.nip.io:<port>/`. Live on every load (reads SQLite). Filters
for year, month, category, and transaction type. Charts cover the current
selection (“this period”) and longer trends on one scrollable page.

## 2. Goals

- Open a URL, pass HTTP Basic Auth, see a useful finance overview immediately.
- Numbers reflect the live ledger (WAL read-only open on each `/api/data` call).
- Category / month / year / type filters drive every KPI and chart.
- Zero write paths from the web tier — no inserts, updates, deletes, or admin UI.
- Bot process stays unchanged in shape: still no published ports of its own.

## 3. Non-goals

- HTTPS / custom domain / Let’s Encrypt (can add a tunnel later without redesign).
- Editing transactions, confirming categories, or any bot feature in the browser.
- Multi-user accounts or per-user data partitioning.
- Server-side filter endpoints (v1 loads the ledger once; browser filters).
- A polished mobile-native app — responsive one-pager is enough.

## 4. Architecture

Two containers, one data directory:

```
bot service          stats service
  no ports             publishes STATS_PORT (default 8080)
  writes DB            mounts ./data read-only
         \             /
          ▼           ▼
       data/finappa.db
```

- **Stats stack:** Starlette + Uvicorn in-process. Same Docker image as the bot;
  different `command` (`python -m stats`).
- **Auth:** HTTP Basic Auth (`STATS_USERNAME` / `STATS_PASSWORD`) on every route.
- **Data API:** `GET /api/data` → JSON list of transactions (plus metadata).
  Page is static files (`GET /`).
- **Filtering & charts:** Client-side (vanilla JS + Chart.js CDN).
- **Escape hatch:** If the full dump ever becomes too large, replace `/api/data`
  with pre-aggregated series; keep the same page shell and filters.

### Compose / ports invariant

`AGENTS.md` previously forbade any `ports:` key in `docker-compose.yml` so the
bot could never accidentally expose an inbound socket. That rule is refined:

- The **bot** service must never gain a `ports:` mapping.
- The **stats** service may publish exactly one host port for the one-pager.
- No other inbound ports.

Docker’s published port bypasses `ufw`; Basic Auth is the access control.

## 5. Page contents

One scrollable page, brand “finappa” as the hero signal.

**Filters (always visible):** year, month (or All), category (or All), type
(All / Expense / Income / Refund). Changing any filter recomputes KPIs + charts
from the in-memory payload (no refetch required).

**This period (top):**

- KPI row: Income, Expenses, Net (EUR, Berlin-local period bounds).
- Spend by category (bar or doughnut) — expenses net of refunds in that category.
- Daily net spend for the selected month (hidden or replaced when month=All).

**Trends (below):**

- Income vs expenses by month (grouped bars) for the selected year (or all years
  if year=All).
- Category mix over months (stacked bars) for expenses.

No raw full transaction table in v1 (keeps the page an analysis surface, not a
spreadsheet). `/export` remains the CSV path.

## 6. Data contract

`GET /api/data` (auth required):

```json
{
  "timezone": "Europe/Berlin",
  "generated_at": "2026-08-11T14:00:00+00:00",
  "transactions": [
    {
      "id": 1,
      "ts": "2026-08-11T10:15:00+00:00",
      "local_date": "2026-08-11",
      "local_year": 2026,
      "local_month": 8,
      "amount_cents": 3000,
      "type": "EXPENSE",
      "category": "Groceries",
      "merchant": "Rewe",
      "notes": null,
      "source": "message"
    }
  ]
}
```

Money stays `amount_cents: int`, always positive; direction is `type`. Local
calendar fields use `settings.timezone` so month filters match `/summary`.

Read path: `sqlite3` URI `file:<path>?mode=ro` — no SQLAlchemy session on the
stats side, so there is no write API to misuse.

## 7. Configuration

| Env | Required | Default | Purpose |
|---|---|---|---|
| `STATS_USERNAME` | yes (for stats) | — | Basic Auth user |
| `STATS_PASSWORD` | yes (for stats) | — | Basic Auth password |
| `STATS_HOST` | no | `0.0.0.0` | Bind address |
| `STATS_PORT` | no | `8080` | Container listen port / published port |
| `DB_PATH` | no | `/app/data/finappa.db` | Same as bot |
| `TIMEZONE` | no | `Europe/Berlin` | Local calendar for filters |

Bot `Settings` does not require the `STATS_*` vars. Stats has its own small
settings object and fails fast at startup if username/password are missing.

## 8. Security

- Read-only DB mount (`:ro`) plus `mode=ro` connection.
- Basic Auth on all routes; `secrets.compare_digest` for password compare.
- No CORS wide-open write methods; only GET.
- Plain HTTP via nip.io — password is base64 on the wire. Accepted for v1
  personal use; HTTPS can be added later (tunnel/Caddy) without changing the app.
- Do not log credentials.

## 9. Testing

- Unit tests for the read/query helper against a real SQLite file (`tmp_path`).
- ASGI tests for 401 without auth, 200 with auth, and JSON shape.
- No network; Chart.js not exercised in pytest (static smoke only).

## 10. Docs to update

- `AGENTS.md` — ports invariant refinement; file map entry for `stats/`.
- `docs/ARCHITECTURE.md` — stats service in the system diagram.
- `docs/backlog.md` — move static stats site out of deferred (done / shipped).
- `README.md` — env vars, compose, nip.io access, how to verify.
- `.env.example` — `STATS_*` placeholders.
