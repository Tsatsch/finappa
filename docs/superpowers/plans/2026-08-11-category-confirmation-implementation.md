# Category Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the LLM is genuinely unsure about a new merchant's category, hold the transaction unlogged and ask the user via Telegram inline buttons instead of guessing.

**Architecture:** One new schema field (`category_confident`) drives a branch in the existing message pipeline: confident/known items log exactly as today, uncertain new-merchant items are held in an in-memory dict and resolved by a new `CallbackQueryHandler` when the user taps a category button.

**Tech Stack:** Same as the existing project — `python-telegram-bot` (adds `InlineKeyboardMarkup`/`InlineKeyboardButton`/`CallbackQueryHandler`, all already part of the existing dependency, no new package), SQLAlchemy, `pytest`.

**Spec:** [docs/superpowers/specs/2026-08-11-category-confirmation-design.md](../specs/2026-08-11-category-confirmation-design.md) — every task below cites the section it implements. This plan modifies files built by [docs/superpowers/plans/2026-08-10-telegram-finance-tracker-implementation.md](2026-08-10-telegram-finance-tracker-implementation.md); read the "Files" block of each task below for the exact current content before editing — line numbers may drift if you don't apply tasks in order.

## Global Constraints

- Money stays `amount_cents: int` everywhere past the LLM boundary (unchanged from the original spec).
- `category_confident: bool` has **no default** on the Pydantic model — OpenAI Structured Outputs strict mode forbids defaults, same constraint that already shaped `currency` (design spec §3).
- Confirmation triggers **only** when `find_matching_merchant` returns `None` (new or merchant-less item) **and** `category_confident` is `False`. A known merchant's stored category always wins regardless of `category_confident` — never re-ask for it (design spec §2).
- Pending state lives in `application.bot_data["pending_confirmations"]: dict[str, PendingConfirmation]` — **in-memory only, never a database table** (design spec §5.1). Losing it on restart costs a resend, never data.
- A held item's `Transaction.ts` is the **original message's arrival time**, captured when it was extracted — never "now" at confirmation time, however much later that is (design spec §5.1).
- `CallbackQueryHandler` does not compose with `filters.User` the way `CommandHandler`/`MessageHandler` do. Auth for the new handler is an explicit `if query.from_user.id != allowed_user_id: return` guard as its first line (design spec §5.4).
- Every module with real logic gets a test using real SQLite (tmp file, never `:memory:`) and either a stubbed OpenAI client (`MagicMock`) or a `SimpleNamespace`/`AsyncMock`-based fake PTB context — the exact pattern already used in `tests/test_main.py` for `recurring_job`/`backup_job`.

---

### Task 1: Schema — `category_confident` on `TransactionItem` (design spec §3)

**Files:**
- Modify: `app/extractor.py:11-17` (`TransactionItem`), `app/extractor.py:33-42` (`SYSTEM_PROMPT`)
- Modify: `tests/test_extractor.py:19-29` (`_item()` helper)
- Test: `tests/test_extractor.py`

**Interfaces:**
- Produces: `app.extractor.TransactionItem.category_confident: bool` — required, no default. Every later task that constructs a `TransactionItem` (directly or via a test helper) must supply it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_extractor.py` (needs a new import: `from pydantic import ValidationError` at the top):

```python
def test_transaction_item_requires_category_confident():
    with pytest.raises(ValidationError):
        TransactionItem(
            amount=30.0, currency="EUR", merchant="Rewe",
            category=CategoryName.GROCERIES, type="EXPENSE", notes=None,
        )


def test_transaction_item_accepts_category_confident_true_and_false():
    confident = _item(category_confident=True)
    unconfident = _item(category_confident=False)
    assert confident.category_confident is True
    assert unconfident.category_confident is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extractor.py::test_transaction_item_requires_category_confident -v`
Expected: FAIL — no `ValidationError` is raised, because the field doesn't exist yet and omitting it is currently harmless.

- [ ] **Step 3: Add the field and update the system prompt**

In `app/extractor.py`, change:

```python
class TransactionItem(BaseModel):
    amount: float
    currency: str
    merchant: str
    category: CategoryName
    type: Literal["EXPENSE", "INCOME", "REFUND"]
    notes: str | None = None
```

to:

```python
class TransactionItem(BaseModel):
    amount: float
    currency: str
    merchant: str
    category: CategoryName
    category_confident: bool
    type: Literal["EXPENSE", "INCOME", "REFUND"]
    notes: str | None = None
```

And append one sentence to `SYSTEM_PROMPT` (still one string, just extend the
concatenation):

```python
SYSTEM_PROMPT = (
    "You extract financial transactions from a message written by the user to "
    "their personal finance tracker. The message may describe one or several "
    "transactions (expenses, income, or refunds). For each transaction, extract "
    "the amount, currency (ISO 4217 code, e.g. EUR, USD), the merchant or "
    "payer/payee name (empty string if there is none, e.g. a salary deposit), "
    "the category (pick the single best fit from the given list), the type "
    "(EXPENSE, INCOME, or REFUND), and any short note worth keeping. If the "
    "message describes no transactions, return an empty list. Also set "
    "category_confident to true or false: false only when the category is a "
    "genuine judgment call between two or more categories, or the merchant "
    "gives no real signal for a category — not merely because the merchant is "
    "unfamiliar to you. Default to true whenever a category is a reasonably "
    "obvious fit."
)
```

- [ ] **Step 4: Update the test helper so every other test in this file keeps constructing valid items**

In `tests/test_extractor.py`, change `_item()`'s defaults:

```python
def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0,
        currency="EUR",
        merchant="Rewe",
        category=CategoryName.GROCERIES,
        category_confident=True,
        type="EXPENSE",
        notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_extractor.py -v`
Expected: all tests pass, including the 2 new ones (10 total).

- [ ] **Step 6: Commit**

```bash
git add app/extractor.py tests/test_extractor.py
git commit -m "Add category_confident to extraction schema and prompt"
```

---

### Task 2: `find_matching_merchant` — read-only fuzzy lookup (design spec §4)

**Files:**
- Modify: `app/categorize.py` (extract shared lookup, refactor `resolve_category` and `recat_merchant` to use it)
- Test: `tests/test_categorize.py`

**Interfaces:**
- Consumes: nothing new — same `Session`/`Merchant`/`Category` as today.
- Produces: `app.categorize.find_matching_merchant(session: Session, merchant_name: str) -> Merchant | None` — no side effects, no creation. Returns `None` for an empty name or no match above `FUZZY_CUTOFF`. Task 3 depends on this exact signature to decide whether an item's merchant is "new."
- `resolve_category`'s and `recat_merchant`'s public signatures and behavior are unchanged — this task is an internal refactor plus one new function.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_categorize.py` (add `find_matching_merchant` to the existing `from app.categorize import ...` line):

```python
def test_find_matching_merchant_returns_none_when_no_match(session):
    assert find_matching_merchant(session, "Rewe") is None


def test_find_matching_merchant_returns_none_for_empty_name(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.commit()
    assert find_matching_merchant(session, "") is None


def test_find_matching_merchant_returns_match(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.commit()
    # Verified: difflib.SequenceMatcher(None, "rewe", "rewwe").ratio() = 0.8889 > 0.85
    merchant = find_matching_merchant(session, "Rewwe")
    assert merchant is not None
    assert merchant.name == "rewe"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_categorize.py -v -k find_matching_merchant`
Expected: FAIL with `ImportError: cannot import name 'find_matching_merchant'`.

- [ ] **Step 3: Extract the helper and refactor both callers**

Replace the full contents of `app/categorize.py` with:

```python
import difflib

from sqlalchemy.orm import Session

from app.categories import CategoryName
from app.db import Category, Merchant, Transaction

FUZZY_CUTOFF = 0.85


class RecatError(Exception):
    """User-facing error for /recat: unknown category or unknown merchant."""


def _normalize(name: str) -> str:
    return name.strip().lower()


def find_matching_merchant(session: Session, merchant_name: str) -> Merchant | None:
    if not merchant_name:
        return None
    normalized = _normalize(merchant_name)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    return known[match[0]] if match else None


def resolve_category(
    session: Session, merchant_name: str, llm_category: CategoryName
) -> tuple[Merchant | None, Category]:
    if not merchant_name:
        category = session.query(Category).filter_by(name=llm_category.value).one()
        return None, category

    existing = find_matching_merchant(session, merchant_name)
    if existing:
        category = session.get(Category, existing.category_id)
        return existing, category

    category = session.query(Category).filter_by(name=llm_category.value).one()
    merchant = Merchant(name=_normalize(merchant_name), display_name=merchant_name, category_id=category.id)
    session.add(merchant)
    session.flush()
    return merchant, category


def recat_merchant(session: Session, merchant_name: str, category_name: str) -> tuple[Merchant, int]:
    try:
        category_enum = CategoryName(category_name)
    except ValueError:
        valid = ", ".join(c.value for c in CategoryName)
        raise RecatError(f"Unknown category '{category_name}'. Valid categories: {valid}")

    merchant = find_matching_merchant(session, merchant_name)
    if merchant is None:
        raise RecatError(f"No merchant matching '{merchant_name}' found.")

    category = session.query(Category).filter_by(name=category_enum.value).one()
    merchant.category_id = category.id

    count = (
        session.query(Transaction)
        .filter_by(merchant_id=merchant.id)
        .update({"category_id": category.id}, synchronize_session="fetch")
    )
    session.commit()
    return merchant, count
```

This also removes duplication `recat_merchant` had accumulated (it had its own
copy of the fuzzy-match lookup from an earlier fix) — both callers now share
one implementation.

- [ ] **Step 4: Run tests to verify everything passes**

Run: `pytest tests/test_categorize.py -v`
Expected: all pass (6 existing + 3 new = 9), including the pre-existing
`resolve_category`/`recat_merchant` tests unchanged — this refactor must not
alter their observable behavior.

- [ ] **Step 5: Commit**

```bash
git add app/categorize.py tests/test_categorize.py
git commit -m "Extract find_matching_merchant, dedupe recat_merchant's lookup"
```

---

### Task 3: Hold uncertain new-merchant items in `process_message` (design spec §5.1, §5.2, §6)

**Files:**
- Modify: `app/handlers.py:1-59` (imports through `process_message`)
- Modify: `tests/test_handlers.py` (existing `process_message` tests + `_item()` helper)
- Test: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `app.categorize.find_matching_merchant` (Task 2); `TransactionItem.category_confident` (Task 1).
- Produces: `app.handlers.PendingConfirmation` (dataclass: `pending_id: str`, `ts: datetime`, `amount_cents: int`, `type: str`, `merchant: str`, `notes: str | None`).
- Produces: `app.handlers.ProcessResult` (dataclass: `reply_text: str | None`, `pending: list[PendingConfirmation]`).
- **Breaking change:** `app.handlers.process_message` now returns `ProcessResult` instead of `str`. Every caller (Task 4's `handle_message`, and every existing test) must be updated. `reply_text` is `None` when every item in the message was held for confirmation (no "Logged 0" message).

- [ ] **Step 1: Update the shared test helper first**

In `tests/test_handlers.py`, change `_item()`'s defaults the same way as Task 1
(this file has its own independent copy of the helper):

```python
def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0, currency="EUR", merchant="Rewe",
        category=CategoryName.GROCERIES, category_confident=True,
        type="EXPENSE", notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)
```

Add `Merchant` to the existing `from app.db import Category, Transaction, get_sessionmaker, init_db, make_engine`
line (becomes `from app.db import Category, Merchant, Transaction, get_sessionmaker, init_db, make_engine`).

- [ ] **Step 2: Update the three existing `process_message` tests for the new return type**

Replace:

```python
def test_process_message_logs_and_replies(session_factory):
    client = _stub_client(_item())
    reply = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=42
    )

    assert "Logged 1" in reply
    assert "Rewe" in reply
    with session_factory() as session:
        txn = session.query(Transaction).one()
        assert txn.amount_cents == 3000
        assert txn.source_update_id == 42
```

with:

```python
def test_process_message_logs_and_replies(session_factory):
    client = _stub_client(_item())
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=42
    )

    assert "Logged 1" in result.reply_text
    assert "Rewe" in result.reply_text
    assert result.pending == []
    with session_factory() as session:
        txn = session.query(Transaction).one()
        assert txn.amount_cents == 3000
        assert txn.source_update_id == 42
```

Replace:

```python
def test_process_message_rejection_logs_nothing(session_factory):
    client = _stub_client(_item(currency="USD"))
    reply = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30 USD Rewe", update_id=1
    )

    assert "EUR" in reply
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
```

with:

```python
def test_process_message_rejection_logs_nothing(session_factory):
    client = _stub_client(_item(currency="USD"))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30 USD Rewe", update_id=1
    )

    assert "EUR" in result.reply_text
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
```

Replace:

```python
def test_process_message_multi_item_rejection_logs_nothing(session_factory):
    # AGENTS.md invariant: one invalid item in a multi-item message rejects the
    # whole message. Regression guard for the case a single-item test can't
    # cover — the valid item must not get logged just because it came first.
    client = _stub_client(_item(), _item(currency="USD", merchant="Shell"))
    reply = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe and 15 USD Shell", update_id=1
    )

    assert "EUR" in reply
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
```

with:

```python
def test_process_message_multi_item_rejection_logs_nothing(session_factory):
    # AGENTS.md invariant: one invalid item in a multi-item message rejects the
    # whole message. Regression guard for the case a single-item test can't
    # cover — the valid item must not get logged just because it came first.
    client = _stub_client(_item(), _item(currency="USD", merchant="Shell"))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe and 15 USD Shell", update_id=1
    )

    assert "EUR" in result.reply_text
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
```

(`test_export_csv_contains_logged_transaction` and
`test_recat_text_updates_and_reports_count` call `process_message(...)` only to
seed data and never inspect its return value — leave those two calls as-is.)

- [ ] **Step 3: Write the new failing tests for the hold behavior**

Add to `tests/test_handlers.py`:

```python
def test_process_message_holds_uncertain_new_merchant(session_factory):
    client = _stub_client(_item(merchant="Obscure Shop LLC", category_confident=False))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ at that place", update_id=1
    )

    assert result.reply_text is None
    assert len(result.pending) == 1
    pending = result.pending[0]
    assert pending.amount_cents == 3000
    assert pending.merchant == "Obscure Shop LLC"
    assert pending.type == "EXPENSE"
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
        assert session.query(Merchant).count() == 0


def test_process_message_mixed_confident_and_uncertain(session_factory):
    client = _stub_client(
        _item(merchant="Rewe", category_confident=True),
        _item(merchant="Obscure Shop LLC", category_confident=False, amount=15.0),
    )
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe and 15€ somewhere", update_id=1
    )

    assert "Logged 1" in result.reply_text
    assert "Rewe" in result.reply_text
    assert len(result.pending) == 1
    assert result.pending[0].merchant == "Obscure Shop LLC"
    with session_factory() as session:
        assert session.query(Transaction).count() == 1


def test_process_message_known_merchant_ignores_low_confidence(session_factory):
    client = _stub_client(_item(merchant="Rewe", category_confident=True))
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    client2 = _stub_client(
        _item(merchant="Rewe", category_confident=False, category=CategoryName.DINING)
    )
    result = process_message(
        session_factory, client2, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe again", update_id=2
    )

    assert result.reply_text is not None
    assert result.pending == []
    with session_factory() as session:
        assert session.query(Transaction).count() == 2
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_handlers.py -v -k process_message`
Expected: FAIL — `process_message` still returns a bare `str`, so
`result.reply_text` raises `AttributeError`, and the hold behavior doesn't
exist yet.

- [ ] **Step 5: Rewrite `process_message`**

In `app/handlers.py`, add `import secrets` and `from dataclasses import dataclass`
to the imports at the top, add `find_matching_merchant` to the existing
`from app.categorize import RecatError, recat_merchant, resolve_category` line,
then replace the current `process_message` function (lines 26-59) with:

```python
@dataclass
class PendingConfirmation:
    pending_id: str
    ts: datetime
    amount_cents: int
    type: str
    merchant: str
    notes: str | None


@dataclass
class ProcessResult:
    reply_text: str | None
    pending: list[PendingConfirmation]


def process_message(
    session_factory, openai_client: OpenAI, openai_model: str, tz_name: str, text: str, update_id: int
) -> ProcessResult:
    try:
        items = extract_transactions(openai_client, openai_model, text)
    except ExtractionError as exc:
        return ProcessResult(reply_text=exc.user_message, pending=[])

    now = datetime.now(timezone.utc)
    lines = []
    pending: list[PendingConfirmation] = []
    with session_factory() as session:
        for item in items:
            amount_cents = round(item.amount * 100)
            existing_merchant = find_matching_merchant(session, item.merchant)

            if existing_merchant is None and not item.category_confident:
                pending.append(
                    PendingConfirmation(
                        pending_id=secrets.token_hex(4),
                        ts=now,
                        amount_cents=amount_cents,
                        type=item.type,
                        merchant=item.merchant,
                        notes=item.notes,
                    )
                )
                continue

            merchant, category = resolve_category(session, item.merchant, item.category)
            session.add(
                Transaction(
                    ts=now,
                    amount_cents=amount_cents,
                    type=item.type,
                    merchant_id=merchant.id if merchant else None,
                    category_id=category.id,
                    notes=item.notes,
                    source="message",
                    source_update_id=update_id,
                )
            )
            label = item.merchant or item.notes or item.type.title()
            lines.append(f"• {format_cents(amount_cents)} · {label} · {category.name}")
        session.commit()

        reply_text = None
        if lines:
            summary = monthly_summary(session, tz_name, at=now)
            header = f"Logged {len(lines)}:"
            net_line = f"Net this month: {format_cents(summary['net_cents'])}"
            reply_text = "\n".join([header, *lines, net_line])

    return ProcessResult(reply_text=reply_text, pending=pending)
```

Note `header = f"Logged {len(lines)}:"` uses `len(lines)`, not `len(items)` —
some items may have gone to `pending` instead of being logged.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_handlers.py -v`
Expected: all pass. `handle_message` (further down in the same file) still
calls the old string-returning `process_message` at this point — that's fixed
in Task 4, so `pytest tests/test_handlers.py -v` is the right scope for this
step (not the full suite yet).

- [ ] **Step 7: Commit**

```bash
git add app/handlers.py tests/test_handlers.py
git commit -m "Hold uncertain new-merchant items instead of auto-logging"
```

---

### Task 4: Confirmation resolution — buttons, handler, wiring (design spec §5.3, §5.4, §6)

**Files:**
- Modify: `app/handlers.py` (new functions, rewrite `handle_message`, extend `register_handlers`)
- Modify: `app/main.py:85-90` (`build_application`'s `bot_data` population)
- Modify: `tests/test_main.py:22-30` (`test_build_application_wires_bot_data_and_jobs`)
- Test: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `app.handlers.PendingConfirmation`, `ProcessResult` (Task 3).
- Produces: `app.handlers.confirm_category(session_factory, pending: PendingConfirmation, category_name: str, tz_name: str, update_id: int) -> str`.
- Produces: `app.handlers.handle_category_confirmation(update, context)` — async, registered via `CallbackQueryHandler(handle_category_confirmation, pattern=r"^pconf:")`.
- `context.bot_data["pending_confirmations"]` must exist (a `dict`) by the time any handler runs — `app/main.py::build_application` is responsible for initializing it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_handlers.py`. First, extend the imports at the top of the
file:

```python
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
```

(`MagicMock` is already imported — just add the `datetime`/`SimpleNamespace`/`AsyncMock` imports alongside it.) Extend the `from app.handlers import ...` line
to also pull in `PendingConfirmation`, `confirm_category`, `handle_category_confirmation`.

```python
def test_confirm_category_inserts_and_reports(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    reply = confirm_category(session_factory, pending, "Shopping", "Europe/Berlin", update_id=99)

    assert "15,00" in reply
    assert "Shopping" in reply
    with session_factory() as session:
        shopping = session.query(Category).filter_by(name="Shopping").one()
        txn = session.query(Transaction).one()
        assert txn.amount_cents == 1500
        assert txn.category_id == shopping.id
        assert txn.source_update_id == 99


def test_confirm_category_uses_pending_ts_not_now(session_factory):
    original_ts = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    pending = PendingConfirmation(
        pending_id="abc123", ts=original_ts, amount_cents=1000,
        type="EXPENSE", merchant="Shop", notes=None,
    )
    confirm_category(session_factory, pending, "Shopping", "Europe/Berlin", update_id=1)
    with session_factory() as session:
        txn = session.query(Transaction).one()
        assert txn.ts == original_ts


def _fake_callback_context(session_factory, pending_map, allowed_user_id=12345):
    settings = SimpleNamespace(allowed_telegram_user_id=allowed_user_id)
    return SimpleNamespace(
        bot_data={
            "session_factory": session_factory,
            "pending_confirmations": pending_map,
            "timezone": "Europe/Berlin",
            "settings": settings,
        }
    )


async def test_handle_category_confirmation_inserts_on_valid_choice(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:abc123:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=999)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    reply_text = query.edit_message_text.call_args[0][0]
    assert "Shopping" in reply_text
    assert "abc123" not in context.bot_data["pending_confirmations"]
    with session_factory() as session:
        assert session.query(Transaction).count() == 1


async def test_handle_category_confirmation_skip_discards_without_logging(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:abc123:SKIP",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1000)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    assert "Discarded" in query.edit_message_text.call_args[0][0]
    with session_factory() as session:
        assert session.query(Transaction).count() == 0


async def test_handle_category_confirmation_expired_pending_id(session_factory):
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:doesnotexist:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1001)
    context = _fake_callback_context(session_factory, {})

    await handle_category_confirmation(update, context)

    assert "expired" in query.edit_message_text.call_args[0][0]


async def test_handle_category_confirmation_ignores_wrong_user(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99999), data="pconf:abc123:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1002)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    query.answer.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    assert "abc123" in context.bot_data["pending_confirmations"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_handlers.py -v -k "confirm_category or handle_category_confirmation"`
Expected: FAIL with `ImportError` — none of `confirm_category`,
`handle_category_confirmation` exist yet.

- [ ] **Step 3: Add the confirmation functions to `app/handlers.py`**

Add `InlineKeyboardButton`, `InlineKeyboardMarkup` to the existing
`from telegram import Update` line (becomes
`from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update`), and
add `CallbackQueryHandler` to the existing
`from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters`
line. Add `from app.categories import CategoryName` to the imports (needed for
`CategoryName(category_name)` and iterating all categories for the keyboard).

Add these functions after `process_message` (i.e. after the code from Task 3,
before `summary_text`):

```python
def confirm_category(
    session_factory, pending: PendingConfirmation, category_name: str, tz_name: str, update_id: int
) -> str:
    category_enum = CategoryName(category_name)
    with session_factory() as session:
        merchant, category = resolve_category(session, pending.merchant, category_enum)
        session.add(
            Transaction(
                ts=pending.ts,
                amount_cents=pending.amount_cents,
                type=pending.type,
                merchant_id=merchant.id if merchant else None,
                category_id=category.id,
                notes=pending.notes,
                source="message",
                source_update_id=update_id,
            )
        )
        session.commit()
        summary = monthly_summary(session, tz_name, at=pending.ts)

    label = pending.merchant or pending.notes or pending.type.title()
    return (
        f"Logged: {format_cents(pending.amount_cents)} · {label} · {category_name}\n"
        f"Net this month: {format_cents(summary['net_cents'])}"
    )


def _pending_prompt_text(pending: PendingConfirmation) -> str:
    label = pending.merchant or pending.notes or pending.type.title()
    return f"Not sure about the category for {format_cents(pending.amount_cents)} · {label} — which fits?"


def _category_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(cat.value, callback_data=f"pconf:{pending_id}:{cat.value}")
        for cat in CategoryName
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Don't log this", callback_data=f"pconf:{pending_id}:SKIP")])
    return InlineKeyboardMarkup(rows)
```

- [ ] **Step 4: Rewrite `handle_message` and add `handle_category_confirmation`**

Replace the existing `handle_message` function with:

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # process_message makes a blocking OpenAI HTTP call and blocking SQLite
    # work; run it off the event loop so a slow/hung OpenAI call doesn't stall
    # polling, other commands, and background jobs.
    result = await asyncio.to_thread(
        process_message,
        context.bot_data["session_factory"],
        context.bot_data["openai_client"],
        context.bot_data["openai_model"],
        context.bot_data["timezone"],
        update.message.text,
        update.update_id,
    )
    if result.reply_text:
        await update.message.reply_text(result.reply_text)
    for pending in result.pending:
        context.bot_data["pending_confirmations"][pending.pending_id] = pending
        await update.message.reply_text(
            _pending_prompt_text(pending), reply_markup=_category_keyboard(pending.pending_id)
        )
```

Add this new handler after `handle_help`:

```python
async def handle_category_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["settings"].allowed_telegram_user_id:
        return
    await query.answer()
    _, pending_id, choice = query.data.split(":", 2)
    pending = context.bot_data["pending_confirmations"].pop(pending_id, None)
    if pending is None:
        await query.edit_message_text("This confirmation expired — just resend the message.")
        return
    if choice == "SKIP":
        label = pending.merchant or pending.notes or "that item"
        await query.edit_message_text(f"Discarded: {label}, not logged.")
        return
    reply = await asyncio.to_thread(
        confirm_category,
        context.bot_data["session_factory"],
        pending,
        choice,
        context.bot_data["timezone"],
        update.update_id,
    )
    await query.edit_message_text(reply)
```

- [ ] **Step 5: Register the new handler**

In `register_handlers`, add one line after the existing `MessageHandler`
registration:

```python
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_category_confirmation, pattern=r"^pconf:"))
```

- [ ] **Step 6: Initialize `pending_confirmations` in `app/main.py`**

In `build_application`, add one line after
`application.bot_data["settings"] = settings`:

```python
    application.bot_data["settings"] = settings
    application.bot_data["pending_confirmations"] = {}
```

- [ ] **Step 7: Extend the existing wiring test**

In `tests/test_main.py`, add one assertion to
`test_build_application_wires_bot_data_and_jobs`:

```python
    assert application.bot_data["pending_confirmations"] == {}
```

(placed alongside the existing `assert "session_factory" in application.bot_data` line.)

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest -v`
Expected: full suite passes. This is the first point the confirmation flow is
fully wired — treat any failure as a real integration bug.

- [ ] **Step 9: Commit**

```bash
git add app/handlers.py app/main.py tests/test_handlers.py tests/test_main.py
git commit -m "Add category-confirmation buttons, handler, and wiring"
```

---

### Task 5: Documentation (design spec §7)

**Files:**
- Modify: `AGENTS.md`

No test — documentation only.

- [ ] **Step 1: Add the new invariant**

In `AGENTS.md`'s "Invariants" section, insert this bullet right after the
`/recat` backfill one (after the line ending "...zero-sum rows." and before
the "No `ports:` key..." bullet):

```markdown
- **Uncertain new-merchant categorization holds for confirmation, never
  guesses.** A new or merchant-less item where the LLM sets
  `category_confident: false` is never inserted until the user taps a
  category button (or "Don't log this") in Telegram. Known merchants and
  confident guesses log immediately, unchanged — see
  `app/handlers.py::process_message`.
```

- [ ] **Step 2: Add the new "do not add" entry**

In AGENTS.md's "Do not add" section, add this bullet at the end of the list:

```markdown
- A DB-backed pending-confirmation table — the in-memory
  `bot_data["pending_confirmations"]` dict is deliberate; losing it on
  restart only costs a resend, never data (see
  `docs/superpowers/specs/2026-08-11-category-confirmation-design.md` §5.1).
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "Document the category-confirmation invariant"
```

---

## Plan self-review

- **Spec coverage:** every section of the design spec (§1–§9) maps to a task:
  §3 schema → Task 1; §4 categorize refactor → Task 2; §5.1/§5.2/§6 hold logic
  → Task 3; §5.3/§5.4 buttons and handler → Task 4; §7 docs → Task 5. §1/§2
  (problem statement, scope) and §8 (testing) are cross-cutting and reflected
  throughout rather than as a standalone task — consistent with how the
  original plan handled the base spec's non-goals sections. §9 (rejected
  alternatives) requires no task by definition.
- **Placeholders:** none — every step has real code or exact file content, no
  TBD/TODO, no "add appropriate handling."
- **Type consistency:** `PendingConfirmation` and `ProcessResult` (Task 3) are
  used with the same field names and types everywhere they're consumed (Task
  4's `confirm_category`, `handle_message`, and all new tests) — verified by
  re-reading each usage against the Task 3 definition while writing Task 4.
  `find_matching_merchant`'s signature (Task 2) matches its one call site in
  Task 3 exactly.
- **Scope:** one cohesive feature across 5 small tasks, each independently
  testable; no decomposition into separate plans needed.

## Execution options

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between
tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using
`superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
