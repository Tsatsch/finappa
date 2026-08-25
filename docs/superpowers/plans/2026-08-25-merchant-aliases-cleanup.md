# Merchant Aliases & `/cleanup` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable merchant aliases, logging-path alias resolution, and a manual `/cleanup` / `/cleanup_undo` flow (Terra proposals, Apply/Skip/Done, same-category guard, session undo) per `docs/superpowers/specs/2026-08-25-merchant-aliases-cleanup-design.md`.

**Architecture:** New tables in SQLite (`merchant_aliases`, `cleanup_rejections`, `cleanup_sessions`, `cleanup_session_merges`). Pure merge/filter/undo logic in `app/cleanup.py`; alias lookup in `app/categorize.py`; heavy-model propose via Structured Outputs; thin Telegram handlers + `pclean:` callbacks (same auth pattern as `pedit:`). Everyday extraction stays on Luna; cleanup uses Terra + medium reasoning.

**Tech Stack:** Python 3.11+, SQLAlchemy/SQLite, pydantic-settings, openai Structured Outputs (`chat.completions.parse`), python-telegram-bot, pytest.

## Global Constraints

- Money is `amount_cents: int`, always positive; direction is `type`.
- EUR only; `Transaction.ts` always UTC.
- Auth: `filters.User(...)` on command handlers; callback handlers check `query.from_user.id` first.
- Merge moves `merchant_id` only — **never** rewrite moved txs’ `category_id`.
- Same-category hard filter on proposals **and** on Apply.
- No auto-merge; no peer-chain product intent; no Alembic; no new dependencies; no bot `ports:`.
- Register `/cleanup` and `/cleanup_undo` in `HELP_TEXT` and `BOT_COMMANDS`.
- Defaults: `OPENAI_MODEL=gpt-5.6-luna`, `OPENAI_REASONING_EFFORT=none`, `OPENAI_MODEL_HEAVY=gpt-5.6-terra`, `OPENAI_REASONING_EFFORT_HEAVY=medium`.

## File map

| File | Responsibility |
|------|----------------|
| `app/config.py`, `.env.example` | Model + reasoning env vars |
| `app/db.py` | New tables |
| `app/categorize.py` | Exact → alias → fuzzy → create; public normalize |
| `app/cleanup.py` | Filter, merge, reject, undo, LLM propose (new) |
| `app/extractor.py` | Optional `reasoning_effort` on parse |
| `app/handlers.py` | `/cleanup`, `/cleanup_undo`, `pclean:` callbacks |
| `app/main.py` | `bot_data` heavy model + reasoning + `pending_cleanups` |
| `tests/test_config.py`, `test_db.py`, `test_categorize.py`, `test_cleanup.py`, `test_main.py`, `test_handlers.py` | Coverage |

---

### Task 1: Config defaults + `.env.example`

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`
- Modify: `tests/test_main.py` (default `openai_model` assertion)

**Interfaces:**
- Produces on `Settings`:
  - `openai_model: str = "gpt-5.6-luna"`
  - `openai_reasoning_effort: str = "none"`
  - `openai_model_heavy: str = "gpt-5.6-terra"`
  - `openai_reasoning_effort_heavy: str = "medium"`

- [ ] **Step 1: Write failing tests**

In `tests/test_config.py`, change the default assertion and add:

```python
def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.openai_reasoning_effort == "none"
    assert settings.openai_model_heavy == "gpt-5.6-terra"
    assert settings.openai_reasoning_effort_heavy == "medium"
    assert settings.timezone == "Europe/Berlin"
    assert settings.backup_channel_id is None


def test_openai_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_MODEL_HEAVY", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT_HEAVY", "high")
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.openai_reasoning_effort == "low"
    assert settings.openai_model_heavy == "gpt-5.6-sol"
    assert settings.openai_reasoning_effort_heavy == "high"
```

In `tests/test_main.py`, update any `assert application.bot_data["openai_model"] == "gpt-4o-mini"` to `"gpt-5.6-luna"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py::test_settings_load_with_defaults tests/test_config.py::test_openai_model_overrides -v`  
Expected: FAIL on default model / missing attributes.

- [ ] **Step 3: Implement**

`app/config.py` — add the four fields with defaults above; keep existing backup validator.

`.env.example` — replace the OpenAI block with:

```bash
OPENAI_API_KEY=

# Everyday extraction / categorization (high volume).
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=none

# Rare /cleanup alias proposals (Apply/Skip gated).
OPENAI_MODEL_HEAVY=gpt-5.6-terra
OPENAI_REASONING_EFFORT_HEAVY=medium
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py tests/test_main.py -v`  
Expected: PASS (fix any other `gpt-4o-mini` default assertions the same way).

- [ ] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_config.py tests/test_main.py
git commit -m "$(cat <<'EOF'
Default OpenAI models to Luna/Terra with reasoning env vars.

EOF
)"
```

---

### Task 2: Database tables

**Files:**
- Modify: `app/db.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Produces ORM models: `MerchantAlias`, `CleanupRejection`, `CleanupSession`, `CleanupSessionMerge`
- `init_db` / `create_all` creates the new tables (no Alembic)

- [ ] **Step 1: Write failing tests in `tests/test_db.py`**

```python
def test_merchant_alias_and_cleanup_tables_exist(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "merchant_aliases" in names
    assert "cleanup_rejections" in names
    assert "cleanup_sessions" in names
    assert "cleanup_session_merges" in names


def test_cleanup_rejection_pair_unique(tmp_path):
    from datetime import datetime, timezone

    from app.db import CleanupRejection, get_sessionmaker

    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    with Session() as session:
        session.add(
            CleanupRejection(
                left_normalized="a", right_normalized="b", created_at=now
            )
        )
        session.commit()
    with pytest.raises(IntegrityError):
        with Session() as session:
            session.add(
                CleanupRejection(
                    left_normalized="a", right_normalized="b", created_at=now
                )
            )
            session.commit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::test_merchant_alias_and_cleanup_tables_exist tests/test_db.py::test_cleanup_rejection_pair_unique -v`  
Expected: FAIL (tables / model missing).

- [ ] **Step 3: Implement models in `app/db.py`**

Add imports: `UniqueConstraint`, and keep existing imports.

```python
class MerchantAlias(Base):
    __tablename__ = "merchant_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    alias_normalized: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class CleanupRejection(Base):
    __tablename__ = "cleanup_rejections"
    __table_args__ = (
        UniqueConstraint(
            "left_normalized", "right_normalized", name="uq_cleanup_rejection_pair"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    left_normalized: Mapped[str] = mapped_column(String, nullable=False)
    right_normalized: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class CleanupSession(Base):
    __tablename__ = "cleanup_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CleanupSessionMerge(Base):
    __tablename__ = "cleanup_session_merges"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("cleanup_sessions.id"), nullable=False, index=True
    )
    canonical_merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id"), nullable=False
    )
    absorbed_normalized: Mapped[str] = mapped_column(String, nullable=False)
    absorbed_display_name: Mapped[str] = mapped_column(String, nullable=False)
    absorbed_category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    alias_normalized: Mapped[str] = mapped_column(String, nullable=False)
    tx_ids_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of ints
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "$(cat <<'EOF'
Add merchant alias and cleanup session tables.

EOF
)"
```

---

### Task 3: Alias resolution on the logging path

**Files:**
- Modify: `app/categorize.py`
- Modify: `tests/test_categorize.py`

**Interfaces:**
- Produces: `normalize_merchant_name(name: str) -> str` (public; same as today’s `_normalize`)
- Updates: `find_matching_merchant` order: exact `Merchant.name` → `MerchantAlias` → fuzzy ≥ 0.85
- Consumes: `MerchantAlias` from `app.db`

- [ ] **Step 1: Write failing tests**

```python
from app.categorize import normalize_merchant_name, resolve_category
from app.db import MerchantAlias


def test_normalize_merchant_name():
    assert normalize_merchant_name("  Rewe City ") == "rewe city"


def test_alias_resolves_before_creating_new_merchant(session):
    merchant, groceries = resolve_category(session, "Rewe", CategoryName.GROCERIES)
    session.add(
        MerchantAlias(
            alias_normalized="rewe city",
            merchant_id=merchant.id,
        )
    )
    session.commit()

    hit, category = resolve_category(session, "Rewe City", CategoryName.DINING)
    assert hit.id == merchant.id
    assert category.name == "Groceries"  # canonical category wins
    assert session.query(Merchant).filter_by(name="rewe city").count() == 0
```

Keep existing fuzzy / new-merchant tests green.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_categorize.py::test_normalize_merchant_name tests/test_categorize.py::test_alias_resolves_before_creating_new_merchant -v`  
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def normalize_merchant_name(name: str) -> str:
    return name.strip().lower()


def find_matching_merchant(session: Session, merchant_name: str) -> Merchant | None:
    if not merchant_name:
        return None
    normalized = normalize_merchant_name(merchant_name)
    exact = session.query(Merchant).filter_by(name=normalized).one_or_none()
    if exact is not None:
        return exact
    alias = session.query(MerchantAlias).filter_by(alias_normalized=normalized).one_or_none()
    if alias is not None:
        return session.get(Merchant, alias.merchant_id)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    return known[match[0]] if match else None
```

Replace internal `_normalize(...)` calls with `normalize_merchant_name`. Import `MerchantAlias`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_categorize.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/categorize.py tests/test_categorize.py
git commit -m "$(cat <<'EOF'
Resolve merchant aliases before fuzzy match on log.

EOF
)"
```

---

### Task 4: Merge, filter, reject, undo (no LLM)

**Files:**
- Create: `app/cleanup.py`
- Create: `tests/test_cleanup.py`

**Interfaces:**
- Produces:
  - `@dataclass CleanupProposal: from_name: str; to_name: str; reason: str`
  - `rejection_pair(a: str, b: str) -> tuple[str, str]` — sorted normalized pair
  - `filter_proposals(session, proposals: list[CleanupProposal]) -> list[CleanupProposal]`
  - `start_cleanup_session(session) -> CleanupSession`
  - `apply_merge(session, session_id: int, from_name: str, to_name: str) -> str` — success message; raises `CleanupError` on guard failure
  - `reject_proposal(session, from_name: str, to_name: str) -> None`
  - `undo_last_cleanup_session(session) -> str` — message; raises `CleanupError` if nothing to undo
  - `class CleanupError(Exception)` with user-facing `str`

- [ ] **Step 1: Write failing tests in `tests/test_cleanup.py`**

Use the same `session` fixture pattern as `tests/test_categorize.py`.

```python
from datetime import datetime, timezone

import pytest

from app.categories import CategoryName
from app.categorize import resolve_category
from app.cleanup import (
    CleanupError,
    CleanupProposal,
    apply_merge,
    filter_proposals,
    reject_proposal,
    start_cleanup_session,
    undo_last_cleanup_session,
)
from app.db import (
    Category,
    CleanupSession,
    Merchant,
    MerchantAlias,
    Transaction,
    get_sessionmaker,
    init_db,
    make_engine,
)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def _two_rewe_variants(session):
    rewe, groceries = resolve_category(session, "Rewe", CategoryName.GROCERIES)
    city, _ = resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    now = datetime.now(timezone.utc)
    session.add(
        Transaction(
            ts=now,
            amount_cents=500,
            type="EXPENSE",
            merchant_id=city.id,
            category_id=groceries.id,
            source="message",
        )
    )
    session.commit()
    return rewe, city, groceries


def test_filter_drops_cross_category(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()
    kept = filter_proposals(
        session,
        [
            CleanupProposal("Cafe", "Rewe", "nope"),
            CleanupProposal("Rewe City", "Rewe", "variant"),
        ],
    )
    assert kept == []


def test_filter_keeps_same_category_pair(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    kept = filter_proposals(
        session, [CleanupProposal("Rewe City", "Rewe", "variant")]
    )
    assert len(kept) == 1
    assert kept[0].from_name == "Rewe City"


def test_apply_merge_moves_txs_keeps_category_creates_alias(session):
    rewe, city, groceries = _two_rewe_variants(session)
    dining = session.query(Category).filter_by(name="Dining").one()
    tx = session.query(Transaction).filter_by(merchant_id=city.id).one()
    tx.category_id = dining.id
    session.commit()

    sess = start_cleanup_session(session)
    msg = apply_merge(session, sess.id, "Rewe City", "Rewe")

    assert "Merged" in msg
    assert "Rewe City" in msg and "Rewe" in msg
    assert session.get(Merchant, city.id) is None
    assert (
        session.query(MerchantAlias).filter_by(alias_normalized="rewe city").one().merchant_id
        == rewe.id
    )
    tx = session.query(Transaction).one()
    assert tx.merchant_id == rewe.id
    assert tx.category_id == dining.id


def test_apply_merge_rejects_cross_category(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()
    sess = start_cleanup_session(session)
    with pytest.raises(CleanupError, match="categor"):
        apply_merge(session, sess.id, "Cafe", "Rewe")


def test_reject_then_filter_excludes_pair(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    reject_proposal(session, "Rewe City", "Rewe")
    kept = filter_proposals(
        session, [CleanupProposal("Rewe City", "Rewe", "variant")]
    )
    assert kept == []


def test_undo_restores_absorbed_merchant_and_txs(session):
    rewe, city, groceries = _two_rewe_variants(session)
    sess = start_cleanup_session(session)
    apply_merge(session, sess.id, "Rewe City", "Rewe")

    msg = undo_last_cleanup_session(session)
    assert "undo" in msg.lower() or "restored" in msg.lower()

    restored = session.query(Merchant).filter_by(name="rewe city").one()
    assert restored.display_name == "Rewe City"
    assert restored.category_id == groceries.id
    assert session.query(MerchantAlias).filter_by(alias_normalized="rewe city").count() == 0
    tx = session.query(Transaction).one()
    assert tx.merchant_id == restored.id
    assert session.get(CleanupSession, sess.id).undone_at is not None


def test_undo_when_nothing_raises(session):
    with pytest.raises(CleanupError):
        undo_last_cleanup_session(session)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cleanup.py -v`  
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `app/cleanup.py`**

Core behaviors:

1. Resolve merchants by `normalize_merchant_name` against `Merchant.name`, else by case-insensitive `display_name` match among current merchants.
2. `filter_proposals`: both exist; different ids; same `category_id`; pair not in `cleanup_rejections`.
3. `apply_merge`: re-check same category; collect tx ids where `merchant_id == absorbed.id`; update those `merchant_id`s only; retarget `MerchantAlias.merchant_id` from absorbed → canonical; insert alias for `absorbed.name`; delete absorbed; append `CleanupSessionMerge` with `tx_ids_json=json.dumps(ids)`; return `Merged {display_from} → {display_to} ({n} transactions).`
4. `reject_proposal`: insert sorted pair into `CleanupRejection` (ignore duplicate integrity if already present).
5. `undo_last_cleanup_session`: latest `CleanupSession` with `undone_at IS NULL` ordered by `id` desc; for each merge row in reverse: recreate `Merchant(name=absorbed_normalized, display_name=..., category_id=...)`; move listed tx ids back; delete alias row for `alias_normalized`; set session `undone_at`. Do **not** delete rejection rows.

**Commit policy:** helpers call `session.commit()` like `recat_merchant` (tests above assume that).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cleanup.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup.py tests/test_cleanup.py
git commit -m "$(cat <<'EOF'
Add merchant merge, reject, filter, and cleanup undo.

EOF
)"
```

---

### Task 5: LLM propose + extractor reasoning kwargs

**Files:**
- Modify: `app/cleanup.py`
- Modify: `app/extractor.py`
- Modify: `tests/test_cleanup.py`
- Modify: `tests/test_extractor.py` if signatures need updates

**Interfaces:**
- Produces: `propose_merges(client: OpenAI, model: str, reasoning_effort: str, session: Session, *, max_proposals: int = 20) -> list[CleanupProposal]`
- Produces: pydantic models `CleanupProposeItem` (`from_name`, `to_name`, `reason`) and `CleanupProposeResult` (`proposals: list[CleanupProposeItem]`)
- Produces: `_cleanup_parse(...)` internal helper
- Updates: `_client_parse` / `extract_transactions` accept optional `reasoning_effort: str | None = None` and pass through to `parse(...)` when not None (retry without on `TypeError`)

- [ ] **Step 1: Write failing test for propose filter integration (stub client)**

```python
class _FakeCompletion:
    def __init__(self, parsed):
        self.choices = [type("C", (), {"message": type("M", (), {"parsed": parsed})()})]


def test_propose_merges_filters_model_output(session, monkeypatch):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()

    from app import cleanup as cleanup_mod

    def fake_parse(**kwargs):
        assert kwargs["model"] == "gpt-5.6-terra"
        parsed = cleanup_mod.CleanupProposeResult(
            proposals=[
                cleanup_mod.CleanupProposeItem(
                    from_name="Rewe City", to_name="Rewe", reason="variant"
                ),
                cleanup_mod.CleanupProposeItem(
                    from_name="Cafe", to_name="Rewe", reason="bad"
                ),
            ]
        )
        return _FakeCompletion(parsed)

    monkeypatch.setattr(cleanup_mod, "_cleanup_parse", lambda **kw: fake_parse(**kw))

    proposals = cleanup_mod.propose_merges(
        type("Client", (), {})(), "gpt-5.6-terra", "medium", session, max_proposals=20
    )
    assert len(proposals) == 1
    assert proposals[0].from_name == "Rewe City"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cleanup.py::test_propose_merges_filters_model_output -v`  
Expected: FAIL.

- [ ] **Step 3: Implement propose + reasoning passthrough**

In `app/cleanup.py`:

- System prompt: variant/brand aliases only; never peer chains (e.g. Rewe↔Edeka); only suggest pairs from the provided merchant list.
- User payload: list of merchants with display_name, normalized name, category name, tx count.
- `_cleanup_parse(client, model, reasoning_effort, messages, response_format)` → `chat.completions.parse` with `reasoning_effort=...`; on `TypeError`, retry without the kwarg.
- `propose_merges`: call model → map to `CleanupProposal` → `filter_proposals` → slice `[:max_proposals]`.

In `app/extractor.py`:

```python
def _client_parse(
    client: OpenAI, model: str, text: str, reasoning_effort: str | None = None
) -> ExtractionResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    parse = getattr(client.chat.completions, "parse", None)
    if parse is None:  # pragma: no cover
        parse = client.beta.chat.completions.parse
    kwargs = dict(model=model, messages=messages, response_format=ExtractionResult)
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    try:
        completion = parse(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        completion = parse(**kwargs)
    return completion.choices[0].message.parsed


def extract_transactions(
    client: OpenAI,
    model: str,
    text: str,
    tz_name: str = "Europe/Berlin",
    reasoning_effort: str | None = None,
) -> list[TransactionItem]:
    try:
        result = _client_parse(client, model, text, reasoning_effort=reasoning_effort)
        return validate_extraction(result, tz_name)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"Couldn't reach the AI extractor ({exc.__class__.__name__}). "
            "Nothing logged — try again."
        ) from exc
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cleanup.py tests/test_extractor.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup.py app/extractor.py tests/test_cleanup.py tests/test_extractor.py
git commit -m "$(cat <<'EOF'
Propose cleanup merges via heavy model with server-side filters.

EOF
)"
```

---

### Task 6: Telegram `/cleanup`, `/cleanup_undo`, and `pclean:` callbacks

**Files:**
- Modify: `app/handlers.py`
- Modify: `app/main.py`
- Modify: `tests/test_handlers.py` and/or `tests/test_main.py`

**Interfaces:**
- Consumes: `propose_merges`, `start_cleanup_session`, `apply_merge`, `reject_proposal`, `undo_last_cleanup_session`, `CleanupError`, `CleanupProposal`
- Produces handlers: `handle_cleanup`, `handle_cleanup_undo`, `handle_cleanup_confirmation`
- `bot_data["pending_cleanups"][token]` holds: `session_id`, `proposals: list[CleanupProposal]`, `index: int`, `applied: int`, `skipped: int`
- Callback data: `pclean:{token}:{APPLY|SKIP|DONE}`
- Optional helper: `format_cleanup_prompt(proposal, index, total) -> str`

- [ ] **Step 1: Write failing tests**

```python
def test_bot_commands_include_cleanup():
    from app.handlers import BOT_COMMANDS

    names = {c.command for c in BOT_COMMANDS}
    assert "cleanup" in names
    assert "cleanup_undo" in names
```

Also assert `format_cleanup_prompt` content if extracted to `app/cleanup.py`:

```python
def test_format_cleanup_prompt():
    from app.cleanup import CleanupProposal, format_cleanup_prompt

    text = format_cleanup_prompt(
        CleanupProposal("Rewe City", "Rewe", "Same chain variant"), 0, 3
    )
    assert "1/3" in text
    assert "Rewe City" in text and "Rewe" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_handlers.py::test_bot_commands_include_cleanup -v`  
Expected: FAIL.

- [ ] **Step 3: Implement handlers**

`handle_cleanup`:
1. Auth via `filters.User`.
2. Open session via `session_factory`; call `propose_merges` with heavy model + effort from `bot_data` (use `asyncio.to_thread`).
3. If empty → “Nothing to clean up.”
4. Else `start_cleanup_session`, store pending token, send first proposal with keyboard Apply / Skip / Done.

`handle_cleanup_confirmation`:
1. Check `query.from_user.id` first; `query.answer()`.
2. Parse `pclean:{token}:{choice}`.
3. Missing pending → “This cleanup expired — run /cleanup again.”
4. APPLY → `apply_merge`; bump applied; show merge line; advance or finish.
5. SKIP → `reject_proposal`; bump skipped; advance or finish.
6. DONE → clear pending; summary `Cleanup done: {applied} applied, {skipped} skipped.` Remaining proposals are **not** rejected.
7. After last APPLY/SKIP with no remaining → same summary.

`handle_cleanup_undo`:
- `undo_last_cleanup_session` → reply; on `CleanupError` send its message.

Wire `CommandHandler("cleanup"...)`, `CommandHandler("cleanup_undo"...)`, `CallbackQueryHandler(..., pattern=r"^pclean:")`.

Update `HELP_TEXT` with one line each for cleanup / cleanup_undo.

`app/main.py`:

```python
application.bot_data["openai_model"] = settings.openai_model
application.bot_data["openai_reasoning_effort"] = settings.openai_reasoning_effort
application.bot_data["openai_model_heavy"] = settings.openai_model_heavy
application.bot_data["openai_reasoning_effort_heavy"] = settings.openai_reasoning_effort_heavy
application.bot_data["pending_cleanups"] = {}
```

Update `process_message` / `handle_message` to pass `reasoning_effort` from `bot_data` into `extract_transactions`. Update call sites/tests that invoke `process_message`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_handlers.py tests/test_main.py tests/test_cleanup.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/handlers.py app/main.py tests/test_handlers.py tests/test_main.py
git commit -m "$(cat <<'EOF'
Add /cleanup and /cleanup_undo Telegram flow.

EOF
)"
```

---

### Task 7: Spec status + full verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-merchant-aliases-cleanup-design.md` — set `Status: Approved`

- [ ] **Step 1: Mark spec approved**

Change header `Status: Draft (awaiting user review)` → `Status: Approved`.

- [ ] **Step 2: Run full suite**

Run: `pytest -v`  
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-merchant-aliases-cleanup-design.md
git commit -m "$(cat <<'EOF'
Mark merchant-aliases cleanup spec approved.

EOF
)"
```

---

## Spec coverage checklist

| Spec section | Task |
|--------------|------|
| §3 Models & config / `.env.example` | Task 1 |
| §4 Data model | Task 2 |
| §5 Logging alias order | Task 3 |
| §6 UX Apply/Skip/Done, commands, auth | Task 6 |
| §7 Merge (no category rewrite) | Task 4 |
| §8 Non-repetition (alias + rejection) | Task 3 + 4 |
| §9 Same-category guard | Task 4 (+ propose filter in 5) |
| Heavy propose LLM | Task 5 |
| Invariants / no new deps | Global + all tasks |

## Self-review notes

- No TBD placeholders left in tasks.
- `CleanupProposeResult` vs `CleanupProposal` naming is fixed in Task 5 — filter input remains `CleanupProposal`.
- Helpers’ commit policy called out in Task 4.
- Stats site needs **no** code changes: it already joins `Merchant.display_name`; after merge, txs point at canonical.
