# Reliability & Ledger UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backup staleness alerts + `/backup-status`, startup missed-recurring DMs, `/summary [YYYY-MM]`, and `/find <query>` per `docs/superpowers/specs/2026-08-25-reliability-and-ledger-ux-design.md`.

**Architecture:** Keep logic in plain testable functions (`app/backup.py`, `app/recurring.py`, `app/ledger.py`, `app/handlers.py`); thin JobQueue/handler wrappers in `app/main.py` / `app/handlers.py`. Newest local `backup-YYYY-MM-DD.db` is the backup health signal — no new tables.

**Tech Stack:** Python 3.11+, python-telegram-bot JobQueue, SQLAlchemy/SQLite, existing `telegram_send`, pytest.

## Global Constraints

- Money is `amount_cents: int`, always positive; direction is `type`.
- EUR only; `Transaction.ts` always UTC; month bounds via `month_bounds_utc`.
- Auth: `filters.User(...)` on every new command handler.
- Recurring: exact-day fire only; missed notify never inserts; ledger is idempotency state.
- Every JobQueue job: `job_kwargs={"misfire_grace_time": None}`.
- Register new/changed commands in `HELP_TEXT` **and** `BOT_COMMANDS`.
- No new dependencies, no bot `ports:`, no Alembic.
- Prefer `telegram_send` for user DMs from jobs/handlers (existing pattern).

## File map

| File | Responsibility |
|------|----------------|
| `app/backup.py` | Snapshot listing, stale check, status text |
| `app/recurring.py` | `missed_recurring_rules(...)` notify-only query |
| `app/ledger.py` | `find_text(...)` search |
| `app/analytics.py` | Optional tiny helper for parsing `YYYY-MM` → `at` datetime (or keep parse in handlers) |
| `app/handlers.py` | `summary_text` month arg, `/backup-status`, `/find`, HELP/BOT_COMMANDS |
| `app/main.py` | `backup_health_job`, startup missed-recurring after fire, noon daily health job |
| `.claude/skills/restore-backup/SKILL.md` | Quarterly verify section |
| `docs/backlog.md` | Mark package shipped; fix stale “transaction management pending” |
| `tests/test_backup.py`, `test_recurring.py`, `test_handlers.py` / `test_ledger.py`, `test_main.py` | Coverage |

---

### Task 1: Backup stale detection + status text

**Files:**
- Modify: `app/backup.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces:
  - `newest_snapshot(snapshot_dir: str) -> tuple[Path, date] | None`
  - `backup_age_days(snapshot_date: date, today: date) -> int`
  - `is_backup_stale(snapshot_dir: str, today: date | None = None, tz_name: str = "Europe/Berlin") -> bool`
  - `stale_backup_message(snapshot_dir: str, today: date | None = None, tz_name: str = "Europe/Berlin") -> str | None` — `None` if OK; else DM body
  - `backup_status_text(snapshot_dir: str, *, channel_configured: bool, key_configured: bool, today: date | None = None, tz_name: str = "Europe/Berlin") -> str`

- [ ] **Step 1: Write failing tests in `tests/test_backup.py`**

```python
from datetime import date
from pathlib import Path

from app.backup import backup_status_text, is_backup_stale, stale_backup_message


def test_is_backup_stale_false_when_age_is_two_days(tmp_path):
    (tmp_path / "backup-2026-08-23.db").write_text("x")
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is False


def test_is_backup_stale_true_when_age_is_three_days(tmp_path):
    (tmp_path / "backup-2026-08-22.db").write_text("x")
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is True


def test_is_backup_stale_true_when_no_snapshots(tmp_path):
    assert is_backup_stale(str(tmp_path), today=date(2026, 8, 25)) is True


def test_stale_backup_message_none_when_fresh(tmp_path):
    (tmp_path / "backup-2026-08-24.db").write_text("x")
    assert stale_backup_message(str(tmp_path), today=date(2026, 8, 25)) is None


def test_stale_backup_message_when_missing(tmp_path):
    msg = stale_backup_message(str(tmp_path), today=date(2026, 8, 25))
    assert msg == "Backup overdue: no local snapshots found."


def test_backup_status_text_reports_ok_and_flags(tmp_path):
    (tmp_path / "backup-2026-08-24.db").write_text("x")
    text = backup_status_text(
        str(tmp_path),
        channel_configured=True,
        key_configured=True,
        today=date(2026, 8, 25),
    )
    assert "2026-08-24" in text
    assert "1 day" in text or "1 days" in text or "age: 1" in text.lower()
    assert "OK" in text
    assert "channel: yes" in text.lower()
    assert "key: yes" in text.lower()
```

Use consistent status copy in implementation (see Step 3).

- [ ] **Step 2: Run tests — expect FAIL (imports missing)**

Run: `pytest tests/test_backup.py -v --tb=short`

- [ ] **Step 3: Implement in `app/backup.py`**

```python
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ... existing imports/functions ...

_SNAPSHOT_RE = "backup-*.db"  # glob; parse date from stem backup-YYYY-MM-DD


def newest_snapshot(snapshot_dir: str) -> tuple[Path, date] | None:
    best: tuple[Path, date] | None = None
    for path in Path(snapshot_dir).glob("backup-*.db"):
        stem = path.stem  # backup-YYYY-MM-DD
        try:
            d = date.fromisoformat(stem.removeprefix("backup-"))
        except ValueError:
            continue
        if best is None or d > best[1]:
            best = (path, d)
    return best


def backup_age_days(snapshot_date: date, today: date) -> int:
    return (today - snapshot_date).days


def is_backup_stale(
    snapshot_dir: str,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> bool:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    if newest is None:
        return True
    return backup_age_days(newest[1], today) > 2


def stale_backup_message(
    snapshot_dir: str,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> str | None:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    if newest is None:
        return "Backup overdue: no local snapshots found."
    _, snapshot_date = newest
    age = backup_age_days(snapshot_date, today)
    if age <= 2:
        return None
    return (
        f"Backup overdue: newest local snapshot is {snapshot_date.isoformat()} "
        f"({age} days old)."
    )


def backup_status_text(
    snapshot_dir: str,
    *,
    channel_configured: bool,
    key_configured: bool,
    today: date | None = None,
    tz_name: str = "Europe/Berlin",
) -> str:
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    newest = newest_snapshot(snapshot_dir)
    channel = "yes" if channel_configured else "no"
    key = "yes" if key_configured else "no"
    if newest is None:
        return (
            "Backup status: overdue\n"
            "Newest snapshot: none\n"
            f"Channel configured: {channel}\n"
            f"Encryption key configured: {key}"
        )
    _, snapshot_date = newest
    age = backup_age_days(snapshot_date, today)
    state = "OK" if age <= 2 else "overdue"
    return (
        f"Backup status: {state}\n"
        f"Newest snapshot: {snapshot_date.isoformat()} ({age} day{'s' if age != 1 else ''} old)\n"
        f"Channel configured: {channel}\n"
        f"Encryption key configured: {key}"
    )
```

Adjust the status test assertions to match this exact copy.

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_backup.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/backup.py tests/test_backup.py
git commit -m "Add local backup staleness detection and status text."
```

---

### Task 2: Wire backup health jobs + `/backup-status`

**Files:**
- Modify: `app/main.py`
- Modify: `app/handlers.py` (command + HELP + BOT_COMMANDS)
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `stale_backup_message`, `backup_status_text` from Task 1
- Produces: `async def backup_health_job(context) -> None`

- [ ] **Step 1: Failing test — job names include health checks**

In `tests/test_main.py`, extend `test_build_application_wires_bot_data_and_jobs` (or add a sibling):

```python
assert {
    "recurring_check",
    "recurring_check_startup",
    "daily_backup",
    "backup_health",
    "backup_health_startup",
} <= job_names
```

Also assert unlimited misfire grace for the new jobs in the existing grace-time test.

- [ ] **Step 2: Run — expect FAIL (missing job names)**

- [ ] **Step 3: Implement `backup_health_job` and register daily 12:00 + startup**

```python
async def backup_health_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    snapshot_dir = str(Path(settings.db_path).parent)
    message = stale_backup_message(snapshot_dir, tz_name=settings.timezone)
    if message is None:
        return
    await telegram_send(
        lambda: context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=message,
        ),
        context="backup health",
    )
```

Register:

```python
application.job_queue.run_daily(
    backup_health_job,
    time=_local_time(12, 0, settings.timezone),
    name="backup_health",
    job_kwargs=unlimited_grace,
)
application.job_queue.run_once(
    backup_health_job, when=0, name="backup_health_startup", job_kwargs=unlimited_grace
)
```

- [ ] **Step 4: Add `/backup-status` in handlers**

```python
def backup_status_command_text(settings: Settings) -> str:
    snapshot_dir = str(Path(settings.db_path).parent)
    return backup_status_text(
        snapshot_dir,
        channel_configured=settings.backup_channel_id is not None,
        key_configured=bool(settings.backup_encryption_key),
        tz_name=settings.timezone,
    )


async def handle_backup_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = backup_status_command_text(context.bot_data["settings"])
    await telegram_send(lambda: update.message.reply_text(reply), context="/backup-status")
```

Update `HELP_TEXT` and `BOT_COMMANDS` (e.g. `BotCommand("backup-status", "Newest local backup and config flags")`). Register `CommandHandler("backup_status", ...)` — Telegram command names use underscores in Bot API (`backup_status`); users type `/backup_status`. Prefer `/backup_status` consistently in HELP (not hyphen) to match PTB `CommandHandler` name.

Spec said `/backup-status` with hyphen — Telegram bot commands **cannot contain hyphens** in the Bot API (only `a-z`, `0-9`, `_`). Use **`backup_status`** everywhere (`/backup_status`).

- [ ] **Step 5: Tests pass; commit**

```bash
git add app/main.py app/handlers.py tests/test_main.py
git commit -m "Wire backup health jobs and /backup_status command."
```

---

### Task 3: Missed recurring notify (startup)

**Files:**
- Modify: `app/recurring.py`
- Modify: `app/main.py`
- Test: `tests/test_recurring.py`, `tests/test_main.py` as needed

**Interfaces:**
- Produces: `missed_recurring_rules(session, tz_name, at=None) -> list[RecurringRule]`
- Produces: `format_missed_recurring_message(rules: list[RecurringRule]) -> str`
- Extend startup path: after `run_recurring_check` in a dedicated startup job **or** inside an expanded startup wrapper that fires then notifies.

Prefer a dedicated `async def recurring_startup_job` that:
1. runs current `recurring_job` fire logic (or calls `run_recurring_check` + DMs for fired)
2. then calls `missed_recurring_rules` and DMs if non-empty

Simplest approach matching existing code: keep `recurring_job` as-is for daily + startup fire; add `missed_recurring_job` registered only as `run_once` startup that **only** notifies (does not fire). Order: register fire startup first, then missed startup — both `when=0`. Missed uses `day_of_month < today`, so same-day fire in the other job is fine.

- [ ] **Step 1: Failing tests**

```python
def test_missed_recurring_lists_past_day_without_row(session):
    _add_rule(session, day_of_month=1)
    at = datetime(2026, 3, 15, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    missed = missed_recurring_rules(session, "Europe/Berlin", at=at)
    assert len(missed) == 1
    assert missed[0].label == "Rent"
    assert session.query(Transaction).count() == 0


def test_missed_recurring_empty_when_already_logged(session):
    rule = _add_rule(session, day_of_month=1)
    at = datetime(2026, 3, 15, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    run_recurring_check(session, "Europe/Berlin", at=datetime(2026, 3, 1, 6, 0, tzinfo=ZoneInfo("Europe/Berlin")))
    missed = missed_recurring_rules(session, "Europe/Berlin", at=at)
    assert missed == []


def test_missed_recurring_empty_on_rule_day(session):
    _add_rule(session, day_of_month=15)
    at = datetime(2026, 3, 15, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert missed_recurring_rules(session, "Europe/Berlin", at=at) == []
```

- [ ] **Step 2: Implement**

```python
def missed_recurring_rules(
    session: Session, tz_name: str, at: datetime | None = None
) -> list[RecurringRule]:
    now_utc = at or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    start_utc, end_utc = month_bounds_utc(tz_name, at=now_utc)
    missed = []
    for rule in session.query(RecurringRule).filter_by(active=True).all():
        if rule.day_of_month >= now_local.day:
            continue
        already = (
            session.query(Transaction)
            .filter(
                Transaction.recurring_rule_id == rule.id,
                Transaction.ts >= start_utc,
                Transaction.ts < end_utc,
            )
            .first()
        )
        if already is None:
            missed.append(rule)
    return missed


def format_missed_recurring_message(rules: list[RecurringRule]) -> str:
    lines = [
        "Recurring skipped this month (bot was down on their day — log manually if needed):"
    ]
    for rule in rules:
        lines.append(f"• {rule.label} (day {rule.day_of_month})")
    return "\n".join(lines)
```

- [ ] **Step 3: Wire `missed_recurring_job` startup-only in `main.py`**

```python
async def missed_recurring_job(context: ContextTypes.DEFAULT_TYPE, at: datetime | None = None) -> None:
    session_factory = context.bot_data["session_factory"]
    settings: Settings = context.bot_data["settings"]
    with session_factory() as session:
        missed = missed_recurring_rules(session, settings.timezone, at=at)
        payload = [(r.label, r.day_of_month) for r in missed]
    if not payload:
        return
    # rebuild message from payload to avoid detached instances
    text = "Recurring skipped this month (bot was down on their day — log manually if needed):\n"
    text += "\n".join(f"• {label} (day {day})" for label, day in payload)
    await telegram_send(
        lambda: context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=text,
        ),
        context="missed recurring",
    )
```

Register `run_once(..., name="missed_recurring_startup", ...)`.

- [ ] **Step 4: Tests pass; commit**

```bash
git commit -m "Notify on startup when recurring rules missed their day."
```

---

### Task 4: `/summary [YYYY-MM]`

**Files:**
- Modify: `app/handlers.py`
- Test: `tests/test_handlers.py`

**Interfaces:**
- Change: `summary_text(session_factory, tz_name, month: str | None = None) -> str`
- Parse `YYYY-MM`; reject future vs current Berlin year-month; usage on bad input.

- [ ] **Step 1: Failing tests**

```python
def test_summary_text_for_past_month(session_factory):
    # insert expense in July 2026 Berlin, assert "Summary for 2026-07:" and amount
    ...


def test_summary_text_rejects_future_month(session_factory):
    assert summary_text(session_factory, "Europe/Berlin", month="2099-01") == (
        "That month is in the future."
    )


def test_summary_text_bad_format(session_factory):
    assert summary_text(session_factory, "Europe/Berlin", month="2026-13") == (
        "Usage: /summary [YYYY-MM]"
    )
```

(Implement past-month insert using `ts_from_local_date` like other handler tests.)

- [ ] **Step 2: Implement parse + `handle_summary` using `context.args`**

```python
SUMMARY_USAGE = "Usage: /summary [YYYY-MM]"


def summary_text(session_factory, tz_name: str, month: str | None = None) -> str:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    if month is None:
        with session_factory() as session:
            return format_summary_message(monthly_summary(session, tz_name))
    try:
        year_s, month_s = month.split("-")
        year, mon = int(year_s), int(month_s)
        if not (1 <= mon <= 12) or len(year_s) != 4:
            return SUMMARY_USAGE
        target = date(year, mon, 1)
    except (ValueError, TypeError):
        return SUMMARY_USAGE
    current = date(now_local.year, now_local.month, 1)
    if target > current:
        return "That month is in the future."
    at = datetime(year, mon, 1, 12, 0, tzinfo=tz)
    with session_factory() as session:
        body = format_summary_message(monthly_summary(session, tz_name, at=at))
    if target == current:
        return body
    return f"Summary for {year:04d}-{mon:02d}:\n{body}"
```

Handler:

```python
async def handle_summary(...):
    month = context.args[0] if context.args else None
    if context.args and len(context.args) > 1:
        reply = SUMMARY_USAGE
    else:
        reply = summary_text(..., month=month)
```

Update HELP + BOT_COMMANDS description to mention optional `YYYY-MM`.

- [ ] **Step 3: Pass + commit**

```bash
git commit -m "Allow /summary for a past YYYY-MM month."
```

---

### Task 5: `/find <query>`

**Files:**
- Modify: `app/ledger.py`
- Modify: `app/handlers.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `FIND_MAX = 20`, `find_text(session_factory, tz_name, query: str) -> str`

- [ ] **Step 1: Failing tests** — merchant match, notes match, case-insensitive, empty query usage, no matches, cap 20

- [ ] **Step 2: Implement**

```python
FIND_USAGE = "Usage: /find <query>"
FIND_MAX = 20


def find_text(session_factory, tz_name: str, query: str) -> str:
    q = query.strip()
    if not q:
        return FIND_USAGE
    pattern = f"%{q}%"
    with session_factory() as session:
        rows = (
            session.query(Transaction)
            .outerjoin(Merchant, Transaction.merchant_id == Merchant.id)
            .filter(
                or_(
                    Merchant.display_name.ilike(pattern),
                    Transaction.notes.ilike(pattern),
                )
            )
            .order_by(Transaction.id.desc())
            .limit(FIND_MAX)
            .all()
        )
        if not rows:
            return f"No matches for '{q}'."
        return "\n".join(format_transaction_line(session, tx, tz_name) for tx in rows)
```

(`from sqlalchemy import or_`)

Handler joins `context.args` with spaces for multi-word queries.

- [ ] **Step 3: HELP + BOT_COMMANDS + register; pass; commit**

```bash
git commit -m "Add /find for merchant and note search."
```

---

### Task 6: Docs polish

**Files:**
- Modify: `.claude/skills/restore-backup/SKILL.md`
- Modify: `docs/backlog.md`
- Modify: `.claude/skills/add-command/SKILL.md` only if needed (already mentions BOT_COMMANDS)

- [ ] **Step 1: Add quarterly verify section to restore-backup skill** (decrypt → integrity_check → count → do not swap)

- [ ] **Step 2: Update backlog** — note reliability/ledger UX shipped 2026-08-25; fix “transaction management still pending” line

- [ ] **Step 3: Commit**

```bash
git commit -m "Document backup verify drill and mark UX package shipped."
```

---

### Task 7: Full verification

- [ ] **Step 1:** `pytest -v`
- [ ] **Step 2:** Confirm `BOT_COMMANDS` includes `backup_status`, `find`, and updated `summary`
- [ ] **Step 3:** Final commit only if anything left uncommitted; push if user requested (user asked to commit when done — push unless they deploy from local pull only; prior session pushed — push)

---

## Spec coverage check

| Spec § | Task |
|--------|------|
| 4 Backup health stale + jobs | 1, 2 |
| 4.4 `/backup_status` | 2 |
| 4.5 restore skill | 6 |
| 5 Missed recurring | 3 |
| 6 `/summary [YYYY-MM]` | 4 |
| 7 `/find` | 5 |
| BOT_COMMANDS | 2, 4, 5 |
| Tests | each task + 7 |

**Note:** Spec wrote `/backup-status` with a hyphen; Bot API forbids hyphens in command names — plan uses `/backup_status`.
