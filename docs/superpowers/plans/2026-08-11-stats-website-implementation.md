# Stats Website Implementation Plan

> **For agentic workers:** Execute task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship a read-only, Basic-Auth-protected stats one-pager as a sibling Docker service that live-reads `finappa.db` and charts income/expenses with year/month/category/type filters.

**Architecture:** Starlette+Uvicorn `stats` package in the same image as the bot; compose publishes a port only on the stats service; browser loads `/api/data` once and filters client-side with Chart.js.

**Tech Stack:** Python 3.11+, Starlette, Uvicorn, stdlib `sqlite3`, Chart.js (CDN), pytest + httpx.

**Spec:** [docs/superpowers/specs/2026-08-11-stats-website-design.md](../specs/2026-08-11-stats-website-design.md)

## Global Constraints

- Money is `amount_cents: int`, always positive; direction is `type`.
- Local calendar fields use `TIMEZONE` (default `Europe/Berlin`).
- SQLite opened `mode=ro` only; data volume mounted `:ro`.
- Bot service must never gain `ports:`; only `stats` may publish a port.
- Do not add FastAPI to the bot process; stats is a sibling entrypoint.
- No domain/TLS in v1 — nip.io + Basic Auth over HTTP.

## File map

| Path | Responsibility |
|---|---|
| `stats/__init__.py` | Package marker |
| `stats/__main__.py` | `python -m stats` entry |
| `stats/settings.py` | Stats-only settings |
| `stats/data.py` | Read-only ledger dump |
| `stats/app.py` | Starlette routes + Basic Auth |
| `stats/static/index.html` | One-pager shell |
| `stats/static/styles.css` | Layout / visual design |
| `stats/static/app.js` | Filters + charts |
| `tests/test_stats.py` | Data + HTTP tests |
| `Dockerfile` | COPY `stats/` |
| `docker-compose.yml` | `stats` service + port |
| `pyproject.toml` | starlette, uvicorn, httpx |
| `.env.example`, docs | Config + runbook |

---

### Task 1: Read-only data layer + settings

**Files:**
- Create: `stats/__init__.py`, `stats/settings.py`, `stats/data.py`
- Test: `tests/test_stats.py`

- [x] **Step 1: Failing tests for `fetch_ledger`**

- [x] **Step 2: Implement settings + `fetch_ledger` using `sqlite3` `mode=ro`**

- [x] **Step 3: Tests pass**

---

### Task 2: Starlette app with Basic Auth

**Files:**
- Create: `stats/app.py`, `stats/__main__.py`
- Modify: `tests/test_stats.py`

- [x] **Step 1: Tests for 401 / 200 / JSON shape**

- [x] **Step 2: Implement `create_app`, Basic Auth middleware, `/` + `/api/data`**

- [x] **Step 3: Tests pass**

---

### Task 3: One-pager UI

**Files:**
- Create: `stats/static/index.html`, `stats/static/styles.css`, `stats/static/app.js`

- [x] **Step 1: HTML shell with filter controls and chart canvases**

- [x] **Step 2: CSS — distinctive, non-generic dashboard look**

- [x] **Step 3: JS — fetch data, filters, KPIs, Chart.js charts**

---

### Task 4: Docker, deps, docs, compose

**Files:**
- Modify: `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`
- Modify: `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/backlog.md`

- [x] **Step 1: Add starlette/uvicorn/httpx; COPY stats; compose stats service**

- [x] **Step 2: Document ports exception, env vars, nip.io access**

- [x] **Step 3: Full `pytest -v` green**

- [x] **Step 4: Commit to main**
