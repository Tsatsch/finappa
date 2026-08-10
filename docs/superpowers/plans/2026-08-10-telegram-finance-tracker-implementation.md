# Telegram Personal Finance Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build finappa — a single-user Telegram bot that extracts transactions from free-text messages via LLM, tracks them in SQLite, and answers `/summary` — deployable as one Docker container on Oracle Cloud Always Free.

**Architecture:** One Python process using `python-telegram-bot` long polling (no webhook, no HTTP server), SQLAlchemy over SQLite in WAL mode, OpenAI Structured Outputs for extraction, and PTB's `JobQueue` for the two daily background jobs (recurring check, encrypted backup).

**Tech Stack:** Python 3.11+, `python-telegram-bot[job-queue]`, SQLAlchemy 2.x, `pydantic`/`pydantic-settings`, `openai`, `cryptography` (Fernet), `PyYAML`, `pytest`.

**Spec:** [docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md](../specs/2026-08-10-telegram-finance-tracker-design.md) — every task below cites the section it implements.

## Global Constraints

- Python 3.11+ (spec §Tech Stack; `StrEnum` requires 3.11).
- Money is **always** `amount_cents: int`, never float, once past the LLM boundary (spec §4.1).
- All `Transaction.ts` values are UTC; month boundaries are computed in `settings.timezone` (default `Europe/Berlin`) then converted to UTC (spec §4.2).
- No new dependency for what stdlib does in a few lines — no `thefuzz` (`difflib` instead), no multi-stage Docker build, no Alembic (spec §14).
- No inbound ports anywhere — `docker-compose.yml` must never gain a `ports:` key (spec §13.1).
- `EUR` is the only accepted currency; anything else rejects the whole message (spec §5.3).
- Auth is exactly one line: `filters.User(user_id=settings.allowed_telegram_user_id)` attached to every handler. No custom auth module (spec §2.2).
- Every module with real logic gets a test using real SQLite (tmp file, not `:memory:` — WAL mode needs a real file) and a stubbed OpenAI client. No network calls in tests.

---

### Task 1: Project scaffolding, config, and category enum

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `app/__init__.py`
- Create: `app/categories.py`
- Create: `app/config.py`
- Test: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `app.categories.CategoryName` (StrEnum with 14 members: GROCERIES, DINING, TRANSPORT, FUEL, HOUSING, UTILITIES, HEALTH, SHOPPING, ENTERTAINMENT, SUBSCRIPTIONS, TRAVEL, INVESTMENT, SALARY, OTHER — values are the display strings e.g. `"Groceries"`).
- Produces: `app.config.Settings` (pydantic-settings `BaseSettings` subclass) and `app.config.get_settings() -> Settings`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "finappa"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "python-telegram-bot[job-queue]>=21.6",
    "sqlalchemy>=2.0",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "openai>=1.40",
    "cryptography>=42.0",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.2", "pytest-asyncio>=0.23"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Write `.gitignore`**

```
data/
.env
__pycache__/
*.pyc
.venv/
*.egg-info/
.pytest_cache/
```

- [ ] **Step 3: Create `app/__init__.py` and `tests/__init__.py` (empty files)**

- [ ] **Step 4: Write `app/categories.py`**

```python
from enum import StrEnum


class CategoryName(StrEnum):
    GROCERIES = "Groceries"
    DINING = "Dining"
    TRANSPORT = "Transport"
    FUEL = "Fuel"
    HOUSING = "Housing"
    UTILITIES = "Utilities"
    HEALTH = "Health"
    SHOPPING = "Shopping"
    ENTERTAINMENT = "Entertainment"
    SUBSCRIPTIONS = "Subscriptions"
    TRAVEL = "Travel"
    INVESTMENT = "Investment"
    SALARY = "Salary"
    OTHER = "Other"
```

- [ ] **Step 5: Write the failing test for config validation**

```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

from app.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.timezone == "Europe/Berlin"
    assert settings.backup_channel_id is None


def test_settings_missing_required_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_backup_channel_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BACKUP_CHANNEL_ID", "-1001234567890")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError, match="BACKUP_ENCRYPTION_KEY"):
        Settings(_env_file=None)


def test_backup_channel_with_key_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BACKUP_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "fake-fernet-key")
    settings = Settings(_env_file=None)
    assert settings.backup_channel_id == -1001234567890
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pip install -e ".[dev]" && pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 7: Write `app/config.py`**

```python
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    allowed_telegram_user_id: int
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    backup_channel_id: int | None = None
    backup_encryption_key: str | None = None
    timezone: str = "Europe/Berlin"
    db_path: str = "/app/data/finappa.db"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_backup_key(self) -> "Settings":
        if self.backup_channel_id is not None and not self.backup_encryption_key:
            raise ValueError(
                "BACKUP_ENCRYPTION_KEY is required when BACKUP_CHANNEL_ID is set"
            )
        return self


def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore app/__init__.py app/categories.py app/config.py tests/__init__.py tests/test_config.py
git commit -m "Add project scaffolding, settings, and category enum"
```

---

### Task 2: Database models and WAL-mode engine (spec §4, §2.3)

**Files:**
- Create: `app/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `app.categories.CategoryName` (Task 1).
- Produces: `app.db.Base` (declarative base), `app.db.Category`, `app.db.Merchant`,
  `app.db.Transaction`, `app.db.RecurringRule` (ORM models matching spec §4 exactly —
  field names and types below are load-bearing for every later task).
- Produces: `app.db.make_engine(db_path: str) -> Engine` — WAL mode enabled via a
  `connect` event listener.
- Produces: `app.db.init_db(engine: Engine) -> None` — creates tables and seeds
  `categories` from `CategoryName`, idempotently (safe to call on every startup).
- Produces: `app.db.get_sessionmaker(engine: Engine) -> sessionmaker`.

Column reference (spec §4):

```
Category:        id, name (unique)
Merchant:         id, name (unique, normalized), display_name, category_id (FK), created_at
Transaction:      id, ts (UTC datetime, indexed), amount_cents (int, positive), type
                  ("EXPENSE"|"INCOME"|"REFUND"), merchant_id (FK, nullable),
                  category_id (FK), notes (nullable), source ("message"|"recurring"),
                  source_update_id (int, nullable, indexed), recurring_rule_id (FK, nullable, indexed)
RecurringRule:    id, key (unique), label, amount_cents (int), type, category_id (FK),
                  day_of_month (int, 1..28), active (bool, default True)
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
from sqlalchemy import text

from app.categories import CategoryName
from app.db import Category, init_db, make_engine


def test_wal_mode_enabled(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal"


def test_categories_seeded_from_enum(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = __import__("app.db", fromlist=["get_sessionmaker"]).get_sessionmaker(engine)
    with Session() as session:
        names = {c.name for c in session.query(Category).all()}
    assert names == {c.value for c in CategoryName}


def test_init_db_is_idempotent(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    init_db(engine)  # second call must not duplicate or raise
    Session = __import__("app.db", fromlist=["get_sessionmaker"]).get_sessionmaker(engine)
    with Session() as session:
        count = session.query(Category).count()
    assert count == len(list(CategoryName))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Write `app/db.py`**

```python
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.categories import CategoryName


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_update_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    recurring_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_rules.id"), nullable=True, index=True
    )


class RecurringRule(Base):
    __tablename__ = "recurring_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


def make_engine(db_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_wal(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        existing = {c.name for c in session.query(Category).all()}
        for name in CategoryName:
            if name.value not in existing:
                session.add(Category(name=name.value))
        session.commit()


def get_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "Add SQLAlchemy models with WAL mode and category seeding"
```

---

### Task 3: LLM extraction and boundary validation (spec §5)

**Files:**
- Create: `app/extractor.py`
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `app.categories.CategoryName` (Task 1).
- Produces: `app.extractor.TransactionItem` (pydantic model: `amount: float`,
  `currency: str`, `merchant: str`, `category: CategoryName`,
  `type: Literal["EXPENSE", "INCOME", "REFUND"]`, `notes: str | None = None`).
- Produces: `app.extractor.ExtractionResult` (pydantic model: `transactions: list[TransactionItem]`).
- Produces: `app.extractor.ExtractionError(Exception)` with a `.user_message: str`
  attribute — ready to send to the user verbatim. Raised for **both** API failures
  and validation failures (spec §5.3), so `handlers.py` (Task 9) needs exactly one
  `except` clause.
- Produces: `app.extractor.extract_transactions(client: OpenAI, model: str, text: str) -> list[TransactionItem]`
  — the only function later tasks call. Raises `ExtractionError` on any failure.
- Produces: `app.extractor.validate_extraction(result: ExtractionResult) -> list[TransactionItem]`
  — exposed separately so tests don't need to stub the OpenAI client for validation logic.

Note on the OpenAI SDK call (spec §5.2): the structured-output parse helper's
location has moved between SDK versions (`client.beta.chat.completions.parse` vs.
`client.chat.completions.parse`). Rather than hardcode one, `_client_parse` below
prefers the non-beta path and falls back to `.beta` if it's absent — this is not
speculative flexibility, it's papering over a real, documented SDK version
difference (spec §5.2 explicitly calls this out).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extractor.py
from unittest.mock import MagicMock

import pytest

from app.categories import CategoryName
from app.extractor import (
    ExtractionError,
    ExtractionResult,
    TransactionItem,
    extract_transactions,
    validate_extraction,
)


def _result(*items: TransactionItem) -> ExtractionResult:
    return ExtractionResult(transactions=list(items))


def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0,
        currency="EUR",
        merchant="Rewe",
        category=CategoryName.GROCERIES,
        type="EXPENSE",
        notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)


def test_validate_extraction_passes_valid_items():
    items = validate_extraction(_result(_item()))
    assert len(items) == 1
    assert items[0].merchant == "Rewe"


def test_validate_extraction_rejects_non_eur():
    with pytest.raises(ExtractionError, match="EUR"):
        validate_extraction(_result(_item(currency="USD")))


def test_validate_extraction_rejects_non_positive_amount():
    with pytest.raises(ExtractionError):
        validate_extraction(_result(_item(amount=0)))


def test_validate_extraction_rejects_absurd_amount():
    with pytest.raises(ExtractionError):
        validate_extraction(_result(_item(amount=2_000_000)))


def test_validate_extraction_rejects_empty_result():
    with pytest.raises(ExtractionError, match="didn't find"):
        validate_extraction(_result())


def test_extract_transactions_wraps_api_failure():
    client = MagicMock()
    client.chat.completions.parse.side_effect = TimeoutError("boom")
    with pytest.raises(ExtractionError, match="Couldn't reach"):
        extract_transactions(client, "gpt-4o-mini", "30€ Rewe")


def test_extract_transactions_returns_items_on_success():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=_result(_item())))]
    client.chat.completions.parse.return_value = completion
    items = extract_transactions(client, "gpt-4o-mini", "30€ Rewe")
    assert items[0].merchant == "Rewe"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.extractor'`

- [ ] **Step 3: Write `app/extractor.py`**

```python
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel

from app.categories import CategoryName

MAX_AMOUNT = 1_000_000


class TransactionItem(BaseModel):
    amount: float
    currency: str
    merchant: str
    category: CategoryName
    type: Literal["EXPENSE", "INCOME", "REFUND"]
    notes: str | None = None


class ExtractionResult(BaseModel):
    transactions: list[TransactionItem]


class ExtractionError(Exception):
    """Raised for both OpenAI API failures and boundary-validation failures
    (spec §5.3). `.user_message` is ready to send to the user as-is."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


SYSTEM_PROMPT = (
    "You extract financial transactions from a message written by the user to "
    "their personal finance tracker. The message may describe one or several "
    "transactions (expenses, income, or refunds). For each transaction, extract "
    "the amount, currency (ISO 4217 code, e.g. EUR, USD), the merchant or "
    "payer/payee name (empty string if there is none, e.g. a salary deposit), "
    "the category (pick the single best fit from the given list), the type "
    "(EXPENSE, INCOME, or REFUND), and any short note worth keeping. If the "
    "message describes no transactions, return an empty list."
)


def _client_parse(client: OpenAI, model: str, text: str) -> ExtractionResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    parse = getattr(client.chat.completions, "parse", None)
    if parse is None:  # pragma: no cover - depends on installed openai-python version
        parse = client.beta.chat.completions.parse
    completion = parse(model=model, messages=messages, response_format=ExtractionResult)
    return completion.choices[0].message.parsed


def _fmt_echo_amount(amount: float, currency: str) -> str:
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} €" if currency == "EUR" else f"{formatted} {currency}"


def _echo_items(items: list[TransactionItem]) -> str:
    parts = []
    for item in items:
        label = item.merchant or item.notes or item.type.title()
        parts.append(f"{_fmt_echo_amount(item.amount, item.currency)} {label}")
    return " · ".join(parts)


def validate_extraction(result: ExtractionResult) -> list[TransactionItem]:
    if not result.transactions:
        raise ExtractionError("I didn't find any transactions in that message.")
    for item in result.transactions:
        if item.currency != "EUR":
            raise ExtractionError(
                f"Nothing logged — '{item.merchant or item.notes or 'one item'}' came "
                f"back as {item.currency}, and I only handle EUR.\n"
                f"I read: {_echo_items(result.transactions)}"
            )
        if item.amount <= 0 or item.amount > MAX_AMOUNT:
            raise ExtractionError(
                f"Nothing logged — an amount of {item.amount} doesn't look right.\n"
                f"I read: {_echo_items(result.transactions)}"
            )
    return result.transactions


def extract_transactions(client: OpenAI, model: str, text: str) -> list[TransactionItem]:
    try:
        result = _client_parse(client, model, text)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"Couldn't reach the AI extractor ({exc.__class__.__name__}). "
            "Nothing logged — try again."
        ) from exc
    return validate_extraction(result)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_extractor.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/extractor.py tests/test_extractor.py
git commit -m "Add LLM extraction with all-or-nothing boundary validation"
```

---

### Task 4: Merchant categorization and `/recat` backfill (spec §6.2, §6.3)

**Files:**
- Create: `app/categorize.py`
- Test: `tests/test_categorize.py`

**Interfaces:**
- Consumes: `app.categories.CategoryName` (Task 1); `app.db.Category`, `app.db.Merchant`,
  `app.db.Transaction`, `get_sessionmaker`, `init_db`, `make_engine` (Task 2).
- Produces: `app.categorize.resolve_category(session: Session, merchant_name: str, llm_category: CategoryName) -> tuple[Merchant | None, Category]`
  — the fuzzy-match-or-create step used by the message pipeline (Task 9). Returns
  `(None, category)` when `merchant_name` is empty. Does **not** commit — caller
  controls the transaction boundary.
- Produces: `app.categorize.recat_merchant(session: Session, merchant_name: str, category_name: str) -> tuple[Merchant, int]`
  — repoints the stored mapping and backfills history; returns the merchant and the
  count of transactions updated. Commits internally (this is a standalone command,
  not part of a larger transaction).
- Produces: `app.categorize.RecatError(Exception)` — message is user-facing (unknown
  category or unknown merchant).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_categorize.py
from datetime import datetime, timezone

import pytest

from app.categories import CategoryName
from app.categorize import RecatError, recat_merchant, resolve_category
from app.db import Category, Merchant, Transaction, get_sessionmaker, init_db, make_engine


@pytest.fixture
def session(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def test_new_merchant_uses_llm_category(session):
    merchant, category = resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.commit()
    assert merchant.name == "rewe"
    assert category.name == "Groceries"


def test_fuzzy_match_overrides_llm_category(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.commit()

    # Slightly different spelling; the LLM guesses wrong this time.
    merchant, category = resolve_category(session, "REWE Markt", CategoryName.DINING)
    assert category.name == "Groceries"  # stored mapping wins over the new guess


def test_empty_merchant_has_no_merchant_row(session):
    merchant, category = resolve_category(session, "", CategoryName.SALARY)
    assert merchant is None
    assert category.name == "Salary"


def test_recat_backfills_only_that_merchant(session):
    _, groceries = resolve_category(session, "Rewe", CategoryName.GROCERIES)
    _, dining = resolve_category(session, "McDonalds", CategoryName.DINING)
    session.commit()

    rewe = session.query(Merchant).filter_by(name="rewe").one()
    mcd = session.query(Merchant).filter_by(name="mcdonalds").one()
    now = datetime.now(timezone.utc)

    session.add_all(
        [
            Transaction(
                ts=now, amount_cents=3000, type="EXPENSE",
                merchant_id=rewe.id, category_id=groceries.id, source="message",
            ),
            Transaction(
                ts=now, amount_cents=1500, type="EXPENSE",
                merchant_id=mcd.id, category_id=dining.id, source="message",
            ),
        ]
    )
    session.commit()

    merchant, count = recat_merchant(session, "Rewe", "Dining")
    assert count == 1
    assert merchant.category_id == dining.id

    rewe_txn = session.query(Transaction).filter_by(merchant_id=rewe.id).one()
    mcd_txn = session.query(Transaction).filter_by(merchant_id=mcd.id).one()
    assert rewe_txn.category_id == dining.id
    assert mcd_txn.category_id == dining.id  # was already Dining; untouched by count


def test_recat_unknown_category_raises(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.commit()
    with pytest.raises(RecatError, match="Unknown category"):
        recat_merchant(session, "Rewe", "NotACategory")


def test_recat_unknown_merchant_raises(session):
    with pytest.raises(RecatError, match="No merchant"):
        recat_merchant(session, "Nonexistent", "Dining")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_categorize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.categorize'`

- [ ] **Step 3: Write `app/categorize.py`**

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


def resolve_category(
    session: Session, merchant_name: str, llm_category: CategoryName
) -> tuple[Merchant | None, Category]:
    if not merchant_name:
        category = session.query(Category).filter_by(name=llm_category.value).one()
        return None, category

    normalized = _normalize(merchant_name)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    if match:
        merchant = known[match[0]]
        category = session.get(Category, merchant.category_id)
        return merchant, category

    category = session.query(Category).filter_by(name=llm_category.value).one()
    merchant = Merchant(name=normalized, display_name=merchant_name, category_id=category.id)
    session.add(merchant)
    session.flush()
    return merchant, category


def recat_merchant(session: Session, merchant_name: str, category_name: str) -> tuple[Merchant, int]:
    try:
        category_enum = CategoryName(category_name)
    except ValueError:
        valid = ", ".join(c.value for c in CategoryName)
        raise RecatError(f"Unknown category '{category_name}'. Valid categories: {valid}")

    normalized = _normalize(merchant_name)
    merchant = session.query(Merchant).filter_by(name=normalized).one_or_none()
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

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_categorize.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/categorize.py tests/test_categorize.py
git commit -m "Add merchant fuzzy-match categorization and /recat backfill"
```

---

### Task 5: Analytics — month boundaries and `/summary` math (spec §4.2, §7)

**Files:**
- Create: `app/analytics.py`
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `app.db.Category`, `app.db.Transaction`, `get_sessionmaker`, `init_db`,
  `make_engine` (Task 2).
- Produces: `app.analytics.month_bounds_utc(tz_name: str, at: datetime | None = None) -> tuple[datetime, datetime]`
  — `(start_utc, end_utc)` for the calendar month containing `at` (default now),
  computed in `tz_name` local time then converted to UTC. This is the function
  that gets the Berlin/UTC boundary right or wrong (spec §4.2) — every later
  consumer of "current month" goes through it.
- Produces: `app.analytics.format_cents(cents: int) -> str` — e.g. `2000` →
  `"20,00 €"`, `-41250` → `"−412,50 €"` (note: U+2212 minus, matching spec §6
  example, not a hyphen). Reused by `handlers.py` (Task 8/9).
- Produces: `app.analytics.monthly_summary(session: Session, tz_name: str, at: datetime | None = None) -> dict`
  — `{"income_cents": int, "expense_cents": int, "net_cents": int, "by_category": dict[str, int]}`.
  Refunds reduce `expense_cents` and their category's total (spec §4.1); they are
  never added to income.
- Produces: `app.analytics.format_summary_message(summary: dict) -> str` — the
  text `/summary` sends.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_analytics.py
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analytics import format_cents, month_bounds_utc, monthly_summary
from app.db import Category, Transaction, get_sessionmaker, init_db, make_engine


@pytest.fixture
def session(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def test_month_boundary_at_0030_local_falls_in_correct_month():
    # 00:30 Berlin time on Jan 1 2026 (CET, UTC+1) == 23:30 UTC on Dec 31 2025.
    txn_ts_utc = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc)
    tz = ZoneInfo("Europe/Berlin")

    jan_start, jan_end = month_bounds_utc("Europe/Berlin", at=datetime(2026, 1, 1, 0, 30, tzinfo=tz))
    assert jan_start <= txn_ts_utc < jan_end

    dec_start, dec_end = month_bounds_utc("Europe/Berlin", at=datetime(2025, 12, 15, tzinfo=tz))
    assert not (dec_start <= txn_ts_utc < dec_end)


def test_format_cents_positive_and_negative():
    assert format_cents(2000) == "20,00 €"
    assert format_cents(-41250) == "−412,50 €"


def test_monthly_summary_refund_reduces_expense(session):
    groceries = session.query(Category).filter_by(name="Groceries").one()
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Transaction(ts=now, amount_cents=3000, type="EXPENSE", category_id=groceries.id, source="message"),
            Transaction(ts=now, amount_cents=1000, type="REFUND", category_id=groceries.id, source="message"),
        ]
    )
    session.commit()

    summary = monthly_summary(session, "Europe/Berlin", at=now)
    assert summary["expense_cents"] == 2000
    assert summary["by_category"]["Groceries"] == 2000


def test_monthly_summary_net_arithmetic(session):
    groceries = session.query(Category).filter_by(name="Groceries").one()
    salary = session.query(Category).filter_by(name="Salary").one()
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Transaction(ts=now, amount_cents=3000, type="EXPENSE", category_id=groceries.id, source="message"),
            Transaction(ts=now, amount_cents=5000, type="INCOME", category_id=salary.id, source="message"),
        ]
    )
    session.commit()

    summary = monthly_summary(session, "Europe/Berlin", at=now)
    assert summary["income_cents"] == 5000
    assert summary["expense_cents"] == 3000
    assert summary["net_cents"] == 2000


def test_monthly_summary_excludes_other_months(session):
    groceries = session.query(Category).filter_by(name="Groceries").one()
    last_month = datetime.now(timezone.utc).replace(day=1) - __import__("datetime").timedelta(days=1)
    session.add(
        Transaction(ts=last_month, amount_cents=9999, type="EXPENSE", category_id=groceries.id, source="message")
    )
    session.commit()

    summary = monthly_summary(session, "Europe/Berlin")
    assert summary["expense_cents"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_analytics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.analytics'`

- [ ] **Step 3: Write `app/analytics.py`**

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import Category, Transaction


def month_bounds_utc(tz_name: str, at: datetime | None = None) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    now_local = (at or datetime.now(timezone.utc)).astimezone(tz)
    start_local = datetime(now_local.year, now_local.month, 1, tzinfo=tz)
    if now_local.month == 12:
        next_start_local = datetime(now_local.year + 1, 1, 1, tzinfo=tz)
    else:
        next_start_local = datetime(now_local.year, now_local.month + 1, 1, tzinfo=tz)
    return start_local.astimezone(timezone.utc), next_start_local.astimezone(timezone.utc)


def format_cents(cents: int) -> str:
    sign = "−" if cents < 0 else ""
    formatted = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}{formatted} €"


def monthly_summary(session: Session, tz_name: str, at: datetime | None = None) -> dict:
    start_utc, end_utc = month_bounds_utc(tz_name, at)

    rows = (
        session.query(Transaction.type, Category.name, func.sum(Transaction.amount_cents))
        .join(Category, Transaction.category_id == Category.id)
        .filter(Transaction.ts >= start_utc, Transaction.ts < end_utc)
        .group_by(Transaction.type, Category.name)
        .all()
    )

    income_cents = 0
    expense_cents = 0
    by_category: dict[str, int] = {}

    for type_, category_name, total in rows:
        total = int(total or 0)
        if type_ == "INCOME":
            income_cents += total
        elif type_ == "EXPENSE":
            expense_cents += total
            by_category[category_name] = by_category.get(category_name, 0) + total
        elif type_ == "REFUND":
            expense_cents -= total
            by_category[category_name] = by_category.get(category_name, 0) - total

    return {
        "income_cents": income_cents,
        "expense_cents": expense_cents,
        "net_cents": income_cents - expense_cents,
        "by_category": by_category,
    }


def format_summary_message(summary: dict) -> str:
    lines = [
        f"Income: {format_cents(summary['income_cents'])}",
        f"Expenses: {format_cents(summary['expense_cents'])}",
        f"Net: {format_cents(summary['net_cents'])}",
    ]
    if summary["by_category"]:
        lines.append("")
        lines.append("By category:")
        for name, cents in sorted(summary["by_category"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {format_cents(cents)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_analytics.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/analytics.py tests/test_analytics.py
git commit -m "Add month-boundary math and /summary aggregation"
```

---

### Task 6: Recurring rules — YAML load and idempotent daily check (spec §8.2, §8.3)

**Files:**
- Create: `app/recurring.py`
- Test: `tests/test_recurring.py`

**Interfaces:**
- Consumes: `app.analytics.month_bounds_utc` (Task 5); `app.categories.CategoryName`
  (Task 1); `app.db.Category`, `app.db.RecurringRule`, `app.db.Transaction`,
  `get_sessionmaker`, `init_db`, `make_engine` (Task 2).
- Produces: `app.recurring.load_recurring_rules(session: Session, yaml_path: str) -> None`
  — reads the YAML file and upserts `RecurringRule` rows by `key`. No-ops if the
  file doesn't exist. Raises `ValueError` if any `day_of_month` is outside 1..28.
- Produces: `app.recurring.run_recurring_check(session: Session, tz_name: str, at: datetime | None = None) -> list[RecurringRule]`
  — inserts a `Transaction` for every active rule whose `day_of_month` has arrived
  and isn't already logged this month; returns the rules that fired (caller uses
  this list to send Telegram notifications). The ledger itself is the idempotency
  state — no separate "runs" table (spec §8.2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recurring.py
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import Category, RecurringRule, Transaction, get_sessionmaker, init_db, make_engine
from app.recurring import load_recurring_rules, run_recurring_check


@pytest.fixture
def session(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def _add_rule(session, key="rent", day_of_month=1, amount_cents=120000, active=True):
    category = session.query(Category).filter_by(name="Housing").one()
    rule = RecurringRule(
        key=key, label="Rent", amount_cents=amount_cents, type="EXPENSE",
        category_id=category.id, day_of_month=day_of_month, active=active,
    )
    session.add(rule)
    session.commit()
    return rule


def test_running_twice_inserts_one_row(session):
    _add_rule(session, day_of_month=1)
    at = datetime(2026, 3, 15, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    fired_first = run_recurring_check(session, "Europe/Berlin", at=at)
    fired_second = run_recurring_check(session, "Europe/Berlin", at=at)

    assert len(fired_first) == 1
    assert len(fired_second) == 0
    assert session.query(Transaction).filter_by(source="recurring").count() == 1


def test_day_not_arrived_does_not_fire(session):
    _add_rule(session, day_of_month=20)
    at = datetime(2026, 3, 5, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    fired = run_recurring_check(session, "Europe/Berlin", at=at)

    assert fired == []
    assert session.query(Transaction).count() == 0


def test_missed_day_self_heals_next_run(session):
    _add_rule(session, day_of_month=1)
    # Container was down on the 1st; first check happens on the 5th.
    at = datetime(2026, 3, 5, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    fired = run_recurring_check(session, "Europe/Berlin", at=at)

    assert len(fired) == 1
    assert session.query(Transaction).filter_by(source="recurring").count() == 1


def test_inactive_rule_never_fires(session):
    _add_rule(session, day_of_month=1, active=False)
    at = datetime(2026, 3, 15, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    fired = run_recurring_check(session, "Europe/Berlin", at=at)

    assert fired == []


def test_load_recurring_rules_upserts_by_key(session, tmp_path):
    yaml_path = tmp_path / "recurring.yml"
    yaml_path.write_text(
        "- key: rent\n  label: Rent\n  amount: 1200.00\n  type: EXPENSE\n"
        "  category: Housing\n  day_of_month: 1\n"
    )
    load_recurring_rules(session, str(yaml_path))
    assert session.query(RecurringRule).count() == 1
    assert session.query(RecurringRule).filter_by(key="rent").one().amount_cents == 120000

    # Change the amount and reload; must update in place, not duplicate.
    yaml_path.write_text(
        "- key: rent\n  label: Rent\n  amount: 1300.00\n  type: EXPENSE\n"
        "  category: Housing\n  day_of_month: 1\n"
    )
    load_recurring_rules(session, str(yaml_path))
    assert session.query(RecurringRule).count() == 1
    assert session.query(RecurringRule).filter_by(key="rent").one().amount_cents == 130000


def test_load_recurring_rules_rejects_bad_day_of_month(session, tmp_path):
    yaml_path = tmp_path / "recurring.yml"
    yaml_path.write_text(
        "- key: rent\n  label: Rent\n  amount: 1200.00\n  type: EXPENSE\n"
        "  category: Housing\n  day_of_month: 31\n"
    )
    with pytest.raises(ValueError, match="day_of_month"):
        load_recurring_rules(session, str(yaml_path))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recurring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.recurring'`

- [ ] **Step 3: Write `app/recurring.py`**

```python
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy.orm import Session

from app.analytics import month_bounds_utc
from app.categories import CategoryName
from app.db import Category, RecurringRule, Transaction


def load_recurring_rules(session: Session, yaml_path: str) -> None:
    path = Path(yaml_path)
    if not path.exists():
        return
    entries = yaml.safe_load(path.read_text()) or []
    for entry in entries:
        day_of_month = entry["day_of_month"]
        if not 1 <= day_of_month <= 28:
            raise ValueError(
                f"recurring.yml: '{entry['key']}' day_of_month must be 1..28, got {day_of_month}"
            )
        category = session.query(Category).filter_by(name=CategoryName(entry["category"]).value).one()
        amount_cents = round(entry["amount"] * 100)
        rule = session.query(RecurringRule).filter_by(key=entry["key"]).one_or_none()
        if rule is None:
            rule = RecurringRule(key=entry["key"], category_id=category.id)
            session.add(rule)
        rule.label = entry["label"]
        rule.amount_cents = amount_cents
        rule.type = entry["type"]
        rule.category_id = category.id
        rule.day_of_month = day_of_month
        rule.active = entry.get("active", True)
    session.commit()


def run_recurring_check(
    session: Session, tz_name: str, at: datetime | None = None
) -> list[RecurringRule]:
    now_utc = at or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    start_utc, end_utc = month_bounds_utc(tz_name, at=now_utc)

    fired = []
    for rule in session.query(RecurringRule).filter_by(active=True).all():
        if rule.day_of_month > now_local.day:
            continue
        already_logged = (
            session.query(Transaction)
            .filter(
                Transaction.recurring_rule_id == rule.id,
                Transaction.ts >= start_utc,
                Transaction.ts < end_utc,
            )
            .first()
        )
        if already_logged is not None:
            continue
        session.add(
            Transaction(
                ts=now_utc,
                amount_cents=rule.amount_cents,
                type=rule.type,
                category_id=rule.category_id,
                notes=rule.label,
                source="recurring",
                recurring_rule_id=rule.id,
            )
        )
        fired.append(rule)
    session.commit()
    return fired
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_recurring.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/recurring.py tests/test_recurring.py
git commit -m "Add recurring.yml loader and idempotent daily check"
```

---

### Task 7: Backup — VACUUM INTO, Fernet encryption, pruning (spec §8.1, §13.2)

**Files:**
- Create: `app/backup.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Consumes: `app.db.get_sessionmaker`, `init_db`, `make_engine` (Task 2, test-only).
- Produces: `app.backup.create_snapshot(db_path: str, snapshot_dir: str, at: datetime | None = None) -> Path`
  — `VACUUM INTO` a timestamped `backup-YYYY-MM-DD.db`. **Never `cp`** — the live
  database is in WAL mode and a plain copy can be corrupt.
- Produces: `app.backup.encrypt_file(path: Path, key: str) -> Path` — Fernet-encrypts
  `path`, writes `path.db.enc` alongside it, returns that path.
- Produces: `app.backup.decrypt_file(encrypted_path: Path, key: str, output_path: Path) -> Path`
  — inverse of the above; used by the `restore-backup` skill (Task 13).
- Produces: `app.backup.prune_snapshots(snapshot_dir: str, keep: int = 7) -> list[Path]`
  — deletes all but the newest `keep` `.db` snapshots by filename sort (the
  `YYYY-MM-DD` naming makes lexicographic sort equal chronological sort); returns
  the deleted paths.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backup.py
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from app.backup import create_snapshot, decrypt_file, encrypt_file, prune_snapshots
from app.db import init_db, make_engine


def test_create_snapshot_produces_readable_database(tmp_path):
    db_path = tmp_path / "finappa.db"
    engine = make_engine(str(db_path))
    init_db(engine)

    snapshot_dir = tmp_path / "snapshots"
    at = datetime(2026, 3, 15, tzinfo=timezone.utc)
    snapshot_path = create_snapshot(str(db_path), str(snapshot_dir), at=at)

    assert snapshot_path.name == "backup-2026-03-15.db"
    conn = sqlite3.connect(str(snapshot_path))
    count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()
    assert count == 14


def test_encrypt_decrypt_round_trip(tmp_path):
    db_path = tmp_path / "finappa.db"
    engine = make_engine(str(db_path))
    init_db(engine)
    snapshot_path = create_snapshot(str(db_path), str(tmp_path / "snapshots"))

    key = Fernet.generate_key().decode()
    encrypted_path = encrypt_file(snapshot_path, key)
    assert encrypted_path.exists()

    output_path = tmp_path / "restored.db"
    decrypt_file(encrypted_path, key, output_path)

    assert output_path.read_bytes() == snapshot_path.read_bytes()
    conn = sqlite3.connect(str(output_path))
    count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()
    assert count == 14


def test_prune_snapshots_keeps_newest_n(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    for day in range(1, 11):  # 10 fake snapshots
        (snapshot_dir / f"backup-2026-03-{day:02d}.db").write_text("fake")

    deleted = prune_snapshots(str(snapshot_dir), keep=7)

    remaining = sorted(p.name for p in snapshot_dir.glob("backup-*.db"))
    assert len(remaining) == 7
    assert len(deleted) == 3
    assert remaining == [f"backup-2026-03-{d:02d}.db" for d in range(4, 11)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.backup'`

- [ ] **Step 3: Write `app/backup.py`**

```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def create_snapshot(db_path: str, snapshot_dir: str, at: datetime | None = None) -> Path:
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
    date_str = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    snapshot_path = Path(snapshot_dir) / f"backup-{date_str}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"VACUUM INTO '{snapshot_path}'")
    finally:
        conn.close()
    return snapshot_path


def encrypt_file(path: Path, key: str) -> Path:
    fernet = Fernet(key.encode())
    encrypted_path = path.with_suffix(path.suffix + ".enc")
    encrypted_path.write_bytes(fernet.encrypt(path.read_bytes()))
    return encrypted_path


def decrypt_file(encrypted_path: Path, key: str, output_path: Path) -> Path:
    fernet = Fernet(key.encode())
    output_path.write_bytes(fernet.decrypt(encrypted_path.read_bytes()))
    return output_path


def prune_snapshots(snapshot_dir: str, keep: int = 7) -> list[Path]:
    snapshots = sorted(Path(snapshot_dir).glob("backup-*.db"), key=lambda p: p.name, reverse=True)
    to_delete = snapshots[keep:]
    for path in to_delete:
        path.unlink()
    return to_delete
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backup.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/backup.py tests/test_backup.py
git commit -m "Add VACUUM INTO snapshotting, Fernet encryption, and pruning"
```

---

### Task 8: Handlers — message pipeline and commands (spec §6, §7)

**Files:**
- Create: `app/handlers.py`
- Test: `tests/test_handlers.py`
- Test: `tests/test_undo.py`

**Deviation from the spec's file list:** spec §11 names only `tests/test_undo.py`
for command logic. `handlers.py` also contains `process_message`, `summary_text`,
`export_csv`, and `recat_text`, each with real logic worth a test, so this task
adds `tests/test_handlers.py` alongside the spec-named `test_undo.py` rather than
cramming everything into one file.

**Design note:** every PTB async callback (`handle_message`, `handle_summary`,
etc.) is a thin wrapper — parse the `Update`, call a plain function, `reply_text`
the result. All the logic worth testing lives in those plain functions, which take
a `session_factory` and never touch `telegram.Update`/`Context`. This is what
makes the test file below possible without constructing PTB objects.

**Interfaces:**
- Consumes: `app.analytics.format_cents`, `format_summary_message`, `monthly_summary`
  (Task 5); `app.categorize.RecatError`, `recat_merchant`, `resolve_category`
  (Task 4); `app.db.Category`, `Merchant`, `Transaction`, `get_sessionmaker`
  (Task 2); `app.extractor.ExtractionError`, `extract_transactions` (Task 3).
- Produces: `app.handlers.process_message(session_factory, openai_client, openai_model: str, tz_name: str, text: str, update_id: int) -> str`.
- Produces: `app.handlers.summary_text(session_factory, tz_name: str) -> str`.
- Produces: `app.handlers.undo_last(session_factory) -> str`.
- Produces: `app.handlers.export_csv(session_factory) -> io.BytesIO`.
- Produces: `app.handlers.recat_text(session_factory, merchant_name: str, category_name: str) -> str`.
- Produces: `app.handlers.register_handlers(application: Application, allowed_user_id: int) -> None`.
- **Contract for Task 9:** before `register_handlers` is meaningful, the caller
  must populate `application.bot_data` with exactly these keys:
  `"session_factory"`, `"openai_client"`, `"openai_model"`, `"timezone"`. The
  async callbacks read them from `context.bot_data`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_handlers.py
import csv
import io
from unittest.mock import MagicMock

import pytest

from app.categories import CategoryName
from app.db import Category, Transaction, get_sessionmaker, init_db, make_engine
from app.extractor import ExtractionResult, TransactionItem
from app.handlers import export_csv, process_message, recat_text, summary_text


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    return get_sessionmaker(engine)


def _stub_client(*items: TransactionItem) -> MagicMock:
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [
        MagicMock(message=MagicMock(parsed=ExtractionResult(transactions=list(items))))
    ]
    client.chat.completions.parse.return_value = completion
    return client


def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0, currency="EUR", merchant="Rewe",
        category=CategoryName.GROCERIES, type="EXPENSE", notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)


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


def test_process_message_rejection_logs_nothing(session_factory):
    client = _stub_client(_item(currency="USD"))
    reply = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30 USD Rewe", update_id=1
    )

    assert "EUR" in reply
    with session_factory() as session:
        assert session.query(Transaction).count() == 0


def test_summary_text_reports_zero_on_empty_ledger(session_factory):
    text = summary_text(session_factory, "Europe/Berlin")
    assert "0,00 €" in text


def test_export_csv_contains_logged_transaction(session_factory):
    client = _stub_client(_item())
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    buffer = export_csv(session_factory)
    rows = list(csv.reader(io.TextIOWrapper(buffer, encoding="utf-8")))
    assert rows[0] == ["timestamp", "amount_eur", "type", "category", "merchant", "notes", "source"]
    assert rows[1][1] == "30.00"
    assert rows[1][3] == "Groceries"
    assert rows[1][4] == "Rewe"


def test_recat_text_updates_and_reports_count(session_factory):
    client = _stub_client(_item())
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    reply = recat_text(session_factory, "Rewe", "Dining")

    assert "1 past transaction" in reply
    with session_factory() as session:
        dining = session.query(Category).filter_by(name="Dining").one()
        txn = session.query(Transaction).one()
        assert txn.category_id == dining.id


def test_recat_text_unknown_merchant_reports_error(session_factory):
    reply = recat_text(session_factory, "Nonexistent", "Dining")
    assert "No merchant" in reply
```

```python
# tests/test_undo.py
from datetime import datetime, timezone

import pytest

from app.db import Category, Transaction, get_sessionmaker, init_db, make_engine
from app.handlers import undo_last


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    return get_sessionmaker(engine)


def test_undo_on_empty_ledger_is_noop(session_factory):
    assert undo_last(session_factory) == "Nothing to undo."


def test_undo_removes_only_last_batch(session_factory):
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        groceries = session.query(Category).filter_by(name="Groceries").one()
        session.add_all(
            [
                Transaction(ts=now, amount_cents=1000, type="EXPENSE", category_id=groceries.id,
                            source="message", source_update_id=1),
                Transaction(ts=now, amount_cents=2000, type="EXPENSE", category_id=groceries.id,
                            source="message", source_update_id=2),
                Transaction(ts=now, amount_cents=500, type="EXPENSE", category_id=groceries.id,
                            source="message", source_update_id=2),
            ]
        )
        session.commit()

    undo_last(session_factory)

    with session_factory() as session:
        remaining = session.query(Transaction).all()
        assert len(remaining) == 1
        assert remaining[0].source_update_id == 1


def test_undo_never_removes_recurring_rows(session_factory):
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        housing = session.query(Category).filter_by(name="Housing").one()
        session.add(
            Transaction(ts=now, amount_cents=120000, type="EXPENSE", category_id=housing.id,
                        source="recurring", source_update_id=None)
        )
        session.commit()

    result = undo_last(session_factory)

    assert result == "Nothing to undo."
    with session_factory() as session:
        assert session.query(Transaction).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_handlers.py tests/test_undo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.handlers'`

- [ ] **Step 3: Write `app/handlers.py`**

```python
import csv
import io
from datetime import datetime, timezone

from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.analytics import format_cents, format_summary_message, monthly_summary
from app.categorize import RecatError, recat_merchant, resolve_category
from app.db import Category, Merchant, Transaction, get_sessionmaker
from app.extractor import ExtractionError, extract_transactions

HELP_TEXT = (
    "Send a message describing one or more transactions, e.g. "
    '"30€ Rewe and 15€ fuel".\n\n'
    "Commands:\n"
    "/summary — this month's income, expenses, and net\n"
    "/undo — remove the last message's transactions\n"
    "/export — CSV of all transactions\n"
    "/recat <merchant> <category> — fix a merchant's category, past and future\n"
)


def process_message(
    session_factory, openai_client: OpenAI, openai_model: str, tz_name: str, text: str, update_id: int
) -> str:
    try:
        items = extract_transactions(openai_client, openai_model, text)
    except ExtractionError as exc:
        return exc.user_message

    now = datetime.now(timezone.utc)
    lines = []
    with session_factory() as session:
        for item in items:
            merchant, category = resolve_category(session, item.merchant, item.category)
            amount_cents = round(item.amount * 100)
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
        summary = monthly_summary(session, tz_name, at=now)

    header = f"Logged {len(items)}:"
    net_line = f"Net this month: {format_cents(summary['net_cents'])}"
    return "\n".join([header, *lines, net_line])


def summary_text(session_factory, tz_name: str) -> str:
    with session_factory() as session:
        summary = monthly_summary(session, tz_name)
    return format_summary_message(summary)


def undo_last(session_factory) -> str:
    with session_factory() as session:
        last = (
            session.query(Transaction)
            .filter(Transaction.source_update_id.isnot(None))
            .order_by(Transaction.id.desc())
            .first()
        )
        if last is None:
            return "Nothing to undo."
        update_id = last.source_update_id
        rows = session.query(Transaction).filter_by(source_update_id=update_id).all()
        count = len(rows)
        total_cents = sum(r.amount_cents for r in rows)
        for row in rows:
            session.delete(row)
        session.commit()
    return f"Removed {count} transaction(s) totaling {format_cents(total_cents)}."


def export_csv(session_factory) -> io.BytesIO:
    with session_factory() as session:
        rows = (
            session.query(
                Transaction.ts, Transaction.amount_cents, Transaction.type,
                Category.name, Merchant.display_name, Transaction.notes, Transaction.source,
            )
            .join(Category, Transaction.category_id == Category.id)
            .outerjoin(Merchant, Transaction.merchant_id == Merchant.id)
            .order_by(Transaction.ts)
            .all()
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "amount_eur", "type", "category", "merchant", "notes", "source"])
    for ts, amount_cents, type_, category_name, merchant_name, notes, source in rows:
        writer.writerow(
            [ts.isoformat(), f"{amount_cents / 100:.2f}", type_, category_name, merchant_name or "", notes or "", source]
        )

    return io.BytesIO(buffer.getvalue().encode("utf-8"))


def recat_text(session_factory, merchant_name: str, category_name: str) -> str:
    with session_factory() as session:
        try:
            merchant, count = recat_merchant(session, merchant_name, category_name)
        except RecatError as exc:
            return str(exc)
    return f"{merchant.display_name} → {category_name}, {count} past transaction(s) updated."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = process_message(
        context.bot_data["session_factory"],
        context.bot_data["openai_client"],
        context.bot_data["openai_model"],
        context.bot_data["timezone"],
        update.message.text,
        update.update_id,
    )
    await update.message.reply_text(reply)


async def handle_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = summary_text(context.bot_data["session_factory"], context.bot_data["timezone"])
    await update.message.reply_text(reply)


async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = undo_last(context.bot_data["session_factory"])
    await update.message.reply_text(reply)


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    csv_buffer = export_csv(context.bot_data["session_factory"])
    await update.message.reply_document(document=csv_buffer, filename="finappa_export.csv")


async def handle_recat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /recat <merchant> <category>")
        return
    *merchant_parts, category_name = args
    reply = recat_text(context.bot_data["session_factory"], " ".join(merchant_parts), category_name)
    await update.message.reply_text(reply)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


def register_handlers(application: Application, allowed_user_id: int) -> None:
    user_filter = filters.User(user_id=allowed_user_id)
    application.add_handler(CommandHandler("start", handle_help, filters=user_filter))
    application.add_handler(CommandHandler("help", handle_help, filters=user_filter))
    application.add_handler(CommandHandler("summary", handle_summary, filters=user_filter))
    application.add_handler(CommandHandler("undo", handle_undo, filters=user_filter))
    application.add_handler(CommandHandler("export", handle_export, filters=user_filter))
    application.add_handler(CommandHandler("recat", handle_recat, filters=user_filter))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_message)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_handlers.py tests/test_undo.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add app/handlers.py tests/test_handlers.py tests/test_undo.py
git commit -m "Add message pipeline and command handlers"
```

---

### Task 9: Main — application wiring, jobs, entrypoint (spec §2.3, §8)

**Files:**
- Create: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `app.analytics.format_cents` (Task 5); `app.backup.create_snapshot`,
  `encrypt_file`, `prune_snapshots` (Task 7); `app.config.Settings`, `get_settings`
  (Task 1); `app.db.get_sessionmaker`, `init_db`, `make_engine` (Task 2);
  `app.handlers.register_handlers` (Task 8); `app.recurring.load_recurring_rules`,
  `run_recurring_check` (Task 6).
- Produces: `app.main.build_application(settings: Settings, recurring_yaml_path: str = "recurring.yml") -> Application`
  — wires everything and registers the two jobs; does not start polling.
  `recurring_yaml_path` is a parameter (not a hardcoded constant) specifically so
  tests can point it at a nonexistent path without a working-directory hack.
- Produces: `app.main.main() -> None` — the process entrypoint (`python -m app.main`);
  not unit tested, it only calls `get_settings()` + `build_application()` +
  `application.run_polling()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main.py
import pytest

from app.config import Settings
from app.main import build_application


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-tests")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "finappa.db"))
    return Settings(_env_file=None)


def test_build_application_wires_bot_data_and_jobs(settings, tmp_path):
    application = build_application(settings, recurring_yaml_path=str(tmp_path / "missing.yml"))

    assert application.bot_data["timezone"] == "Europe/Berlin"
    assert application.bot_data["openai_model"] == "gpt-4o-mini"
    assert "session_factory" in application.bot_data

    job_names = {job.name for job in application.job_queue.jobs()}
    assert {"recurring_check", "recurring_check_startup", "daily_backup"} <= job_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Write `app/main.py`**

```python
import logging
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from app.analytics import format_cents
from app.backup import create_snapshot, encrypt_file, prune_snapshots
from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db, make_engine
from app.handlers import register_handlers
from app.recurring import load_recurring_rules, run_recurring_check


def _local_time(hour: int, minute: int, tz_name: str) -> time:
    return time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz_name))


async def recurring_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    session_factory = context.bot_data["session_factory"]
    settings: Settings = context.bot_data["settings"]
    with session_factory() as session:
        fired = run_recurring_check(session, settings.timezone)
    for rule in fired:
        await context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=f"Recurring: logged {rule.label} ({format_cents(rule.amount_cents)}).",
        )


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    snapshot_dir = str(Path(settings.db_path).parent)
    snapshot_path = create_snapshot(settings.db_path, snapshot_dir)

    if settings.backup_channel_id is not None:
        encrypted_path = encrypt_file(snapshot_path, settings.backup_encryption_key)
        try:
            with open(encrypted_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=settings.backup_channel_id,
                    document=f,
                    filename=encrypted_path.name,
                    caption=f"finappa backup {snapshot_path.stem.removeprefix('backup-')}",
                )
        finally:
            encrypted_path.unlink(missing_ok=True)

    prune_snapshots(snapshot_dir, keep=7)


def build_application(settings: Settings, recurring_yaml_path: str = "recurring.yml") -> Application:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        load_recurring_rules(session, recurring_yaml_path)

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["session_factory"] = session_factory
    application.bot_data["openai_client"] = OpenAI(api_key=settings.openai_api_key)
    application.bot_data["openai_model"] = settings.openai_model
    application.bot_data["timezone"] = settings.timezone
    application.bot_data["settings"] = settings

    register_handlers(application, settings.allowed_telegram_user_id)

    application.job_queue.run_daily(
        recurring_job, time=_local_time(6, 0, settings.timezone), name="recurring_check"
    )
    application.job_queue.run_once(recurring_job, when=0, name="recurring_check_startup")
    application.job_queue.run_daily(
        backup_job, time=_local_time(3, 30, settings.timezone), name="daily_backup"
    )

    return application


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_main.py -v`
Expected: 1 passed

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: all tests across every module pass (this is the first point every unit
is wired together — treat any failure here as a real integration bug, not a
fluke).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "Wire application, jobs, and entrypoint"
```

---

### Task 10: `recurring.yml` seed and `.env.example` (spec §8.3, §9)

**Files:**
- Create: `recurring.yml`
- Create: `.env.example`

No test — these are data/template files with no logic; `test_recurring.py` (Task 6)
already covers the loader's parsing and validation.

- [ ] **Step 1: Write `recurring.yml`**

```yaml
# Recurring transactions, checked daily and inserted once per month (spec §8.2).
# day_of_month must be 1..28 so a rule never vanishes in February.
# After editing this file, restart the container to pick up changes.

- key: rent
  label: Rent
  amount: 1200.00
  type: EXPENSE
  category: Housing
  day_of_month: 1

# - key: salary
#   label: Salary
#   amount: 3500.00
#   type: INCOME
#   category: Salary
#   day_of_month: 25
#
# - key: etf_savings_plan
#   label: ETF savings plan
#   amount: 300.00
#   type: EXPENSE
#   category: Investment
#   day_of_month: 5
```

- [ ] **Step 2: Write `.env.example`**

```bash
# Copy to .env and fill in. Never commit the real .env (already gitignored).

# From @BotFather.
TELEGRAM_BOT_TOKEN=

# Your numeric Telegram user ID, from @userinfobot. The bot ignores everyone else.
ALLOWED_TELEGRAM_USER_ID=

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# Negative ID of a PRIVATE Telegram channel where the bot is an admin with post
# rights. Daily encrypted backups are sent here. Leave unset to skip remote
# backups (local snapshots in ./data still happen).
BACKUP_CHANNEL_ID=

# Required if BACKUP_CHANNEL_ID is set. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Store this in a password manager — losing it makes every remote backup
# unrecoverable (spec §13.2).
BACKUP_ENCRYPTION_KEY=

# Drives "current month" boundaries and the two daily jobs' fire times.
TIMEZONE=Europe/Berlin

DB_PATH=/app/data/finappa.db
LOG_LEVEL=INFO
```

- [ ] **Step 3: Commit**

```bash
git add recurring.yml .env.example
git commit -m "Add recurring.yml seed and .env.example"
```

---

### Task 11: `Dockerfile` and `docker-compose.yml` (spec §10, §13.1)

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

No test — verified by the Oracle deploy in Task 15, where the container actually
runs.

**Note on `./data` ownership:** the Dockerfile runs as a non-root `uid 1000` user
(a real security measure, not decoration — spec §13.3 wants the process
unprivileged). Docker bind-mounts `./data` from the host as-is; if the host
directory is freshly created by `docker compose up`, it's typically owned by
`root`, which the container's uid-1000 process cannot write into. README (Task 14)
must tell the operator to `mkdir -p data && sudo chown 1000:1000 data && chmod 700 data`
**before** the first `docker compose up` — this satisfies both the "container can
write" requirement and the "chmod 700" hardening item (spec §13.3) in one step,
since 700 only works if the owning uid matches.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY recurring.yml ./

RUN useradd --create-home --uid 1000 finappa \
    && mkdir -p /app/data \
    && chown -R finappa:finappa /app
USER finappa

CMD ["python", "-m", "app.main"]
```

A multi-stage build is deliberately not used (spec §14): there's no compilation
step for a pure-Python dependency set, so a second stage would only add a
`COPY --from=` line without shrinking anything meaningful.

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
```

No `ports:` key. This must never be added — Docker's own iptables rules bypass
`ufw`, so a published port stays reachable even with the host firewall configured
to deny it (spec §13.1). This constraint is restated in `AGENTS.md` (Task 12) so
it survives future edits by anyone (or anything) working on this repo.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "Add single-stage Dockerfile and docker-compose with no published ports"
```

---

### Task 12: `AGENTS.md`, `CLAUDE.md`, Cursor rules (spec §12)

**Files:**
- Create: `AGENTS.md`
- Create: `CLAUDE.md`
- Create: `.cursor/rules/finappa.mdc`

No test — these are documentation for AI tools, not executable.

- [ ] **Step 1: Write `AGENTS.md`**

```markdown
# AGENTS.md — finappa

A single-user Telegram bot that extracts transactions from free-text messages via
an LLM, tracks them in SQLite, and answers `/summary`. Full design rationale:
`docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md`.

## Architecture in one paragraph

One Python process. `python-telegram-bot` long-polls Telegram (no webhook, no
HTTP server, no inbound port). SQLAlchemy over SQLite in WAL mode is the only
datastore. OpenAI Structured Outputs extracts transactions from free text.
PTB's `JobQueue` runs two daily jobs in-process: a recurring-transaction check and
an encrypted backup upload. Deployed as one Docker container with no `ports:` key.

## Invariants — do not violate these without updating the spec first

- **Money is `amount_cents: int`, always positive.** Direction is `type`
  (`EXPENSE`/`INCOME`/`REFUND`), never a sign. The only float is
  `extractor.TransactionItem.amount`, at the LLM boundary only.
- **EUR only.** Non-EUR rejects the whole message. No FX conversion anywhere.
- **`Transaction.ts` is always UTC.** Month boundaries are computed in
  `settings.timezone` (default `Europe/Berlin`) and converted to UTC before
  querying — see `app/analytics.py::month_bounds_utc`. Get this backwards and a
  purchase near midnight files under the wrong month.
- **Auth is one filter, not a module:** `filters.User(user_id=...)` on every
  handler. There is no `security.py` and there should never be one.
- **The ledger is the recurring-idempotency state.** `run_recurring_check`
  decides whether a rule already fired this month by querying `transactions`
  directly. Do not add a separate "runs" table — it can drift from reality; the
  ledger cannot.
- **All-or-nothing message validation.** One invalid item in a multi-item message
  rejects the whole message. Never log the valid items and drop the rest — the
  user's natural retry would then double-log them.
- **`/recat` backfills history.** It updates the merchant's stored category AND
  every past transaction for that merchant in one statement. Never implement it
  as compensating (reversal + re-entry) transactions — that pollutes every future
  `/summary` and `/export` with zero-sum rows.
- **No `ports:` key in `docker-compose.yml`, ever.** Docker writes its own
  iptables rules that bypass `ufw`; a published port is reachable even with the
  host firewall set to deny it.

## File map

| File | Responsibility |
|---|---|
| `app/config.py` | `pydantic-settings` — all env vars, one validation rule (backup key required if backup channel set) |
| `app/categories.py` | `CategoryName` StrEnum — the only source of truth for valid categories |
| `app/db.py` | SQLAlchemy models, WAL-mode engine, category seeding |
| `app/extractor.py` | OpenAI structured extraction + all-or-nothing boundary validation |
| `app/categorize.py` | Fuzzy merchant→category matching (`difflib`), `/recat` backfill |
| `app/analytics.py` | Berlin/UTC month-boundary math, `/summary` aggregation, money formatting |
| `app/recurring.py` | `recurring.yml` loader, idempotent daily recurring check |
| `app/backup.py` | `VACUUM INTO` snapshotting, Fernet encryption, local pruning |
| `app/handlers.py` | Message pipeline + all Telegram commands (thin async wrappers over plain functions) |
| `app/main.py` | Wires everything, registers the two `JobQueue` jobs, entrypoint |

## Common tasks

- **Add a category:** see `.claude/skills/add-category/SKILL.md`.
- **Add a command:** see `.claude/skills/add-command/SKILL.md`.
- **Deploy a change:** see `.claude/skills/deploy/SKILL.md`.
- **Restore from a backup:** see `.claude/skills/restore-backup/SKILL.md`.
- **Run tests:** `pytest -v` (no network required; the OpenAI client is stubbed
  everywhere except real usage).

## Deploy runbook (also in README.md)

```bash
ssh <host>
cd finappa
git pull
docker compose up -d --build
```

No CI/CD, no registry, no auto-deploy — this is a one-user bot deployed by hand
maybe twice a month. Do not add GitHub Actions deploy automation unless asked.

## Do not add

These were deliberately removed or rejected during design (spec §14) — do not
reintroduce them without a real, stated reason:

- FastAPI, Uvicorn, or any HTTP server — nothing serves HTTP.
- Caddy, a domain, or TLS termination — polling needs none of it.
- A webhook endpoint or `setWebhook` call.
- `Alembic` or any migration framework — one user, one DB, `create_all()` suffices.
- `thefuzz` — `difflib.get_close_matches` already does the one call needed.
- A separate cron container — `JobQueue` runs in-process.
- Google Drive backups — service accounts have no Drive storage quota.
- A regex fast-path before the LLM call — saves under a cent, adds a second code
  path to keep in sync.
- Any new dependency for something a few lines of stdlib already does.
```

- [ ] **Step 2: Write `CLAUDE.md`**

```markdown
# CLAUDE.md

See [AGENTS.md](AGENTS.md) — architecture, invariants, file map, and common
tasks all live there. This file exists only so Claude Code and every other tool
read the same source and can't drift apart.
```

- [ ] **Step 3: Write `.cursor/rules/finappa.mdc`**

```markdown
---
description: finappa architecture and invariants
globs:
alwaysApply: true
---

See [AGENTS.md](../../AGENTS.md) for the full picture. The invariants that most
often get violated by an unaware edit:

- Money is `amount_cents: int`, always positive; direction is `type`, never a sign.
- EUR only — no FX conversion.
- `Transaction.ts` is UTC; month boundaries go through `app/analytics.py::month_bounds_utc`.
- Auth is `filters.User(...)` on every handler — no custom auth module.
- The ledger itself is the recurring-idempotency state — no separate "runs" table.
- `docker-compose.yml` must never gain a `ports:` key.
- Do not add FastAPI, Caddy, webhooks, Alembic, or `thefuzz` — see AGENTS.md's
  "Do not add" list before introducing any new dependency.
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md CLAUDE.md .cursor/rules/finappa.mdc
git commit -m "Add AGENTS.md, CLAUDE.md pointer, and Cursor rules"
```

---

### Task 13: Claude Code project skills (spec §12)

**Files:**
- Create: `.claude/skills/deploy/SKILL.md`
- Create: `.claude/skills/add-category/SKILL.md`
- Create: `.claude/skills/restore-backup/SKILL.md`
- Create: `.claude/skills/add-command/SKILL.md`

No test — these are instructions for an AI tool, not executable code.

- [ ] **Step 1: Write `.claude/skills/deploy/SKILL.md`**

```markdown
---
name: deploy
description: Deploy the latest committed finappa code to the production host. Use when the user asks to deploy, ship, or push finappa changes live.
---

# Deploy finappa

Manual deploy, by design (spec §10) — this is a one-user bot, deployed by hand a
couple of times a month. There is no CI/CD; do not propose adding it here.

1. Confirm the working tree is clean and the change is committed and pushed:
   `git status` and `git log -1`.
2. SSH into the host:
   ```bash
   ssh <user>@<host>
   ```
3. Pull and rebuild:
   ```bash
   cd finappa
   git pull
   docker compose up -d --build
   ```
4. Confirm it started cleanly:
   ```bash
   docker compose logs -f --tail=50
   ```
   Look for the polling loop starting with no traceback. Ctrl+C to stop
   following.
5. Verify from Telegram: send `/summary` to the bot and confirm a reply arrives.
   If nothing replies within ~10 seconds, re-check the logs — a common cause is
   a typo'd `.env` value causing `Settings()` validation to fail at startup, in
   which case the container will be in a restart loop (`docker compose ps` shows
   `Restarting`).
```

- [ ] **Step 2: Write `.claude/skills/add-category/SKILL.md`**

```markdown
---
name: add-category
description: Add a new spending/income category to finappa. Use when the user wants a new category available for transaction classification.
---

# Add a category

`app/categories.py::CategoryName` is the single source of truth (spec §5.1) — it
both seeds the `categories` table and constrains what the LLM is allowed to
return.

1. Add a member to `CategoryName` in `app/categories.py`:
   ```python
   class CategoryName(StrEnum):
       ...
       PETS = "Pets"
   ```
2. No migration needed — `app/db.py::init_db` seeds any category present in the
   enum but missing from the table, and it runs on every startup.
3. Restart the bot (`docker compose up -d --build` in production, or just rerun
   locally) so the new enum value reaches the LLM's schema.
4. Do not remove a category that has existing transactions or merchants pointing
   at it — `category_id` is a required foreign key with no cascade. If a category
   is truly obsolete, leave the enum member in place; it just stops being offered
   for *new* extractions once removed, which would orphan history. Removing one
   safely is out of scope for this skill — ask the user before doing it, since it
   requires reassigning existing rows first.
```

- [ ] **Step 3: Write `.claude/skills/restore-backup/SKILL.md`**

```markdown
---
name: restore-backup
description: Restore finappa's database from an encrypted backup sent to the Telegram backup channel. Use when the host is lost, the local database is corrupted, or the user wants to roll back to a specific day.
---

# Restore from backup

Backups are daily, encrypted, and live in the private Telegram backup channel
(spec §8.1). This is the full path from "found a backup message" to "bot running
on it again."

1. In Telegram, open the backup channel and find the snapshot to restore — each
   message's caption has the date. Download the attached `backup-YYYY-MM-DD.db.enc`
   file to the host (or to your machine, then `scp` it over).
2. Get `BACKUP_ENCRYPTION_KEY` from the password manager — it is **not** recoverable
   from anywhere else. Without it, this specific backup is permanently unreadable
   (spec §13.2); fall back to an older local snapshot in `./data/` if one exists.
3. Decrypt it:
   ```bash
   python -c "
   from pathlib import Path
   from app.backup import decrypt_file
   decrypt_file(Path('backup-2026-08-10.db.enc'), '<BACKUP_ENCRYPTION_KEY>', Path('restored.db'))
   "
   ```
4. Verify it actually opens before trusting it:
   ```bash
   sqlite3 restored.db "PRAGMA integrity_check;"
   sqlite3 restored.db "SELECT COUNT(*) FROM transactions;"
   ```
   `PRAGMA integrity_check` must print `ok`. If it doesn't, try an older snapshot.
5. Stop the bot, swap the file in, restart:
   ```bash
   docker compose down
   cp restored.db ./data/finappa.db
   docker compose up -d
   ```
6. Verify from Telegram: `/summary` should reflect the restored data's month.
```

- [ ] **Step 4: Write `.claude/skills/add-command/SKILL.md`**

```markdown
---
name: add-command
description: Add a new Telegram bot command to finappa. Use when the user wants a new /command added to the bot.
---

# Add a command

`app/handlers.py` keeps every command's real logic in a plain function
(`session_factory` in, string or file out) with a thin `async def handle_*`
wrapper that PTB actually calls. Follow that split — it's what keeps commands
testable without constructing `telegram.Update` objects. See existing commands
(`summary_text`, `undo_last`, `recat_text`) as the pattern to copy.

1. Write the plain function in `app/handlers.py`:
   ```python
   def my_command_text(session_factory, ...) -> str:
       with session_factory() as session:
           ...  # query/mutate
       return "reply text"
   ```
2. Write the thin async wrapper below it:
   ```python
   async def handle_my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
       reply = my_command_text(context.bot_data["session_factory"], ...)
       await update.message.reply_text(reply)
   ```
   Anything the wrapper needs beyond `session_factory` should already be in
   `context.bot_data` (see the keys `build_application` sets in `app/main.py`);
   add a new key there if the command needs something new.
3. Register it in `register_handlers`:
   ```python
   application.add_handler(CommandHandler("mycommand", handle_my_command, filters=user_filter))
   ```
4. Add it to `HELP_TEXT` in `app/handlers.py` so `/help` stays accurate.
5. Write a test for the plain function in `tests/test_handlers.py`, following the
   existing tests' pattern (`session_factory` fixture backed by a `tmp_path`
   SQLite file — never `:memory:`, WAL mode needs a real file).
6. Run `pytest -v`, then deploy (see the `deploy` skill).
```

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/
git commit -m "Add project skills for deploy, categories, backups, and commands"
```

---

### Task 14: `README.md` — setup runbook and host hardening (spec §10.3, §13.3)

**Files:**
- Create: `README.md`

No test — documentation. This task's content is host-agnostic (spec §10.1); the
Oracle-specific provisioning steps are Task 15, which links back here for
everything after "the VM exists."

- [ ] **Step 1: Write `README.md`**

```markdown
# finappa

A single-user Telegram bot that turns free-text messages like
"30€ in Rewe and 15€ for fuel" into structured ledger entries, tracks recurring
transactions (rent, salary, ETF contributions), and answers `/summary`.

Full design rationale: [`docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md`](docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md).
Architecture notes for AI coding tools: [`AGENTS.md`](AGENTS.md).

## Requirements

- A Telegram bot token (from [@BotFather](https://t.me/BotFather)).
- An OpenAI API key.
- Any always-on host that can run Docker and reach the internet outbound — no
  domain, no open ports, no static IP required (spec §10.1). ~150 MB RAM is
  enough.

## One-time setup

1. Create the bot with `@BotFather`; copy the token.
2. Get your numeric Telegram user ID from `@userinfobot`.
3. Create a **private** Telegram channel for backups, add the bot as an admin
   with post rights, and get the channel's ID (forward any message from the
   channel to `@userinfobot`; it starts with `-100`).
4. On the host:
   ```bash
   git clone <this-repo-url> finappa
   cd finappa
   cp .env.example .env
   ```
5. Fill in `.env`: `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_ID`,
   `OPENAI_API_KEY`, `BACKUP_CHANNEL_ID`. Generate `BACKUP_ENCRYPTION_KEY`:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   **Store that key in a password manager.** Losing it makes every remote backup
   permanently unreadable (spec §13.2) — local snapshots on disk are unaffected.
6. Edit `recurring.yml` for your actual rent/salary/etc.
7. Create the data directory with the right owner **before** first start — the
   container runs as uid 1000, and a bind mount created by Docker itself would
   default to root ownership:
   ```bash
   mkdir -p data
   sudo chown 1000:1000 data
   chmod 700 data
   ```
8. Start it:
   ```bash
   docker compose up -d --build
   docker compose logs -f --tail=50
   ```
9. Send `/help` to the bot in Telegram to confirm it's alive.

## Host hardening

The bot itself has no listening ports and no public identity, so the VPS's SSH
is the actual attack surface (spec §13.3). Do this once, on any fresh host:

```bash
# Key-only SSH, no root login
sudo sed -i \
  -e 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
  -e 's/^#*PermitRootLogin.*/PermitRootLogin no/' \
  /etc/ssh/sshd_config
sudo systemctl restart ssh

# Deny all inbound except SSH
sudo ufw default deny incoming
sudo ufw allow 22
sudo ufw enable

# Unattended security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Confirm your SSH key works in a **second** terminal before closing the first —
locking yourself out of a fresh cloud box means a rescue-console trip.

## Deploying a change

```bash
ssh <host>
cd finappa
git pull
docker compose up -d --build
```

See [`.claude/skills/deploy/SKILL.md`](.claude/skills/deploy/SKILL.md) for the
full checklist including how to tell a bad deploy from a good one.

## Restoring from a backup

See [`.claude/skills/restore-backup/SKILL.md`](.claude/skills/restore-backup/SKILL.md).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in test/dummy values
pytest -v
```

Tests never hit the network — the OpenAI client is stubbed everywhere, and
SQLite runs against a `tmp_path` file (WAL mode needs a real file, not
`:memory:`).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Add README with setup runbook and host hardening checklist"
```

---

### Task 15: Provision Oracle Cloud Always Free and deploy (spec §10.2)

**Files:** none — this is an infrastructure task, not code. Nothing to commit.

This task has no unit test; "done" is a live bot that replies in Telegram. Each
step below has an explicit, observable check instead of a `pytest` command —
don't skip those checks, they're the only way to catch a bad step before the next
one compounds it.

- [ ] **Step 1: Create the Always Free compute instance**

In the Oracle Cloud (OCI) Console: **Compute → Instances → Create Instance**.

- **Image and shape:** choose an **Always Free–eligible** shape — the Ampere
  (ARM) `VM.Standard.A1.Flex` shape, sized to **1 OCPU / 6 GB RAM**. Do not take
  the full 2 OCPU / 12 GB allowance on one instance — Oracle's Always Free Ampere
  quota is a **tenancy-wide total**, and leaving headroom means a second free
  instance is possible later without resizing this one. The bot needs
  ~150 MB RAM (spec §10.1); this is generous, not tight.
- **Image:** the latest Ubuntu LTS listed as Always Free–eligible.
- **Networking:** default VCN is fine. Keep "Assign a public IPv4 address"
  checked — the instance still accepts **no unsolicited inbound connections**;
  a public IP is just how you'll SSH in.
- **SSH keys:** generate a new key pair (or upload your own public key). Save
  the private key — Oracle does not store it.
- Create the instance and wait for it to reach the `Running` state.

**Check:** the instance list shows `Running` and a public IP is assigned.

- [ ] **Step 2: Lock down the cloud-level firewall (VCN security list)**

Compute → Instances → (your instance) → **Subnet → Security Lists → Default
Security List**.

Confirm the **only** stateful ingress rule is TCP/22 (SSH), source
`0.0.0.0/0` (or your own IP/32 if it's static — narrower is better, but a bot with
no other exposed service doesn't strictly need it). Delete any other ingress
rule if the quick-create wizard added one (e.g. rules pre-opened for HTTP/HTTPS
you're not using). Leave egress open — the bot needs outbound HTTPS to
`api.telegram.org` and `api.openai.com`.

This is the cloud-level twin of the `docker-compose.yml` "no `ports:` key" rule
(spec §13.1) — two independent layers both saying no inbound service.

**Check:** the security list's ingress rule table has exactly one row (SSH).

- [ ] **Step 3: SSH in and harden the host**

```bash
ssh -i /path/to/private_key ubuntu@<instance-public-ip>
```

Follow **README.md → Host hardening** exactly (key-only SSH, no root login,
`ufw` default-deny plus allow 22, unattended-upgrades). Confirm your key still
works in a **second** terminal before closing the first.

**Check:** `sudo ufw status` shows `Status: active` with only `22/tcp ALLOW`.

- [ ] **Step 4: Install Docker Engine and the Compose plugin**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in (group membership needs a fresh shell) or run
`newgrp docker`.

**Check:** `docker run --rm hello-world` prints Docker's success message, and
`docker compose version` prints a version (not "command not found").

- [ ] **Step 5: Clone and configure finappa**

Follow **README.md → One-time setup**, steps 4 onward (clone, `.env`,
`recurring.yml`, the `data/` ownership step, `docker compose up -d --build`).

**Check:** `docker compose logs -f --tail=50` shows the polling loop start with
no traceback, and `docker compose ps` shows the `bot` service as `Up` (not
`Restarting`).

- [ ] **Step 6: End-to-end verification from Telegram**

From your phone or desktop Telegram client (as the `ALLOWED_TELEGRAM_USER_ID`
user):

1. Send `/help` — expect the help text back within a few seconds.
2. Send `30€ Rewe` — expect a `Logged 1: ... Net this month: ...` confirmation.
3. Send `/summary` — expect it to include the transaction just logged.
4. Send `/undo` — expect a removal confirmation; `/summary` again should no
   longer include it.

**Check:** all four round-trips complete. If any hangs, `docker compose logs`
on the host is the next step — a wrong `.env` value causing `Settings()`
validation to fail at startup is the most common cause, and shows up as a
restart loop.

- [ ] **Step 7: Note the operating caveat**

Oracle may *stop* (not delete) this instance if it stays below 10% CPU and 10%
network for 7 days — this bot is idle by design, so expect it to happen
occasionally (spec §10.2). Recovery is restarting the instance from the OCI
Console; the boot volume and `./data` persist across a stop. If it happens more
than twice, switch to the prepaid-VPS fallback from the spec rather than
converting this instance to Pay-As-You-Go — PAYG removes the stop-on-idle
behavior but reintroduces the unbounded-bill exposure the Always Free choice was
made to avoid.

No commit for this task — infrastructure state lives in the Oracle Console and
on the host, not in git.

---

## Plan self-review

- **Spec coverage:** every numbered section of the design spec (§1–§14) maps to
  at least one task above; the mapping is cited in each task's heading. §11's
  test table maps 1:1 to a test file created in Tasks 3, 4, 5, 6, 7, 8.
- **Placeholders:** none — scanned for TBD/TODO/"implement later"/"add
  appropriate handling"/"similar to Task N" and found none; every step has
  runnable code, real file content, or an observable manual check.
- **Type/signature consistency:** every `Produces` signature (Task 1's
  `CategoryName`/`Settings`; Task 2's models; Task 3's `TransactionItem`/
  `ExtractionResult`/`ExtractionError`; Task 4's `resolve_category`/
  `recat_merchant`; Task 5's `month_bounds_utc`/`format_cents`/
  `monthly_summary`; Task 6's `run_recurring_check`; Task 7's `create_snapshot`/
  `encrypt_file`/`decrypt_file`; Task 9's `build_application`) is used with the
  same name and shape by every later task that consumes it — verified by
  grepping each definition against every call site.
- **Scope:** single subsystem, no decomposition needed — every task builds
  toward the one deployable bot; Task 15 is the only non-code task and is
  explicitly scoped to "the box exists and the bot replies," not ongoing ops.

---

## Execution options

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task,
review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans,
batch execution with checkpoints.

Task 15 (Oracle provisioning) needs real credentials and clicking through the
OCI Console — neither execution mode can do that step for you; whichever we
pick, that task will pause for you to drive it and report back.

Which approach?
