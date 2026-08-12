from datetime import date, datetime, timezone

import pytest

from app.analytics import monthly_summary
from app.categories import CategoryName
from app.db import Category, Merchant, Transaction, get_sessionmaker, init_db, make_engine
from app.extractor import ExtractionError, ExtractionResult, TransactionItem, validate_extraction
from app.ledger import delete_text, edit_text, recent_text


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    return get_sessionmaker(engine)


def _add_expense(session_factory, *, amount_cents=3000, source="message", ts=None, merchant_key="rewe"):
    ts = ts or datetime.now(timezone.utc)
    with session_factory() as session:
        groceries = session.query(Category).filter_by(name="Groceries").one()
        merchant = session.query(Merchant).filter_by(name=merchant_key).one_or_none()
        if merchant is None:
            merchant = Merchant(name=merchant_key, display_name=merchant_key.title(), category_id=groceries.id)
            session.add(merchant)
            session.flush()
        tx = Transaction(
            ts=ts,
            amount_cents=amount_cents,
            type="EXPENSE",
            merchant_id=merchant.id,
            category_id=groceries.id,
            notes=None,
            source=source,
            source_update_id=1 if source == "message" else None,
        )
        session.add(tx)
        session.commit()
        session.refresh(tx)
        return tx.id


def test_validate_extraction_rejects_future_local_date():
    future = date(2099, 1, 1)
    item = TransactionItem(
        amount=30.0,
        currency="EUR",
        merchant="Rewe",
        category=CategoryName.GROCERIES,
        category_confident=True,
        type="EXPENSE",
        notes=None,
        local_date=future,
    )
    with pytest.raises(ExtractionError, match="future"):
        validate_extraction(ExtractionResult(transactions=[item]), "Europe/Berlin")


def test_validate_extraction_accepts_past_local_date():
    item = TransactionItem(
        amount=30.0,
        currency="EUR",
        merchant="Rewe",
        category=CategoryName.GROCERIES,
        category_confident=True,
        type="EXPENSE",
        notes=None,
        local_date=date(2020, 1, 1),
    )
    items = validate_extraction(ExtractionResult(transactions=[item]), "Europe/Berlin")
    assert items[0].local_date == date(2020, 1, 1)


def test_recent_text_lists_newest_first(session_factory):
    _add_expense(session_factory, amount_cents=1000)
    second_id = _add_expense(session_factory, amount_cents=2000)

    text = recent_text(session_factory, "Europe/Berlin", limit=10)
    lines = text.splitlines()
    assert lines[0].startswith(f"#{second_id}")
    assert "10,00 €" in lines[1]
    assert "20,00 €" in lines[0]


def test_recent_text_empty_ledger(session_factory):
    assert recent_text(session_factory, "Europe/Berlin") == "No transactions yet."


def test_delete_text_removes_message_sourced_row(session_factory):
    tx_id = _add_expense(session_factory)
    reply = delete_text(session_factory, "Europe/Berlin", tx_id)
    assert reply.startswith(f"Deleted #{tx_id}")
    with session_factory() as session:
        assert session.get(Transaction, tx_id) is None


def test_delete_text_blocks_recurring(session_factory):
    tx_id = _add_expense(session_factory, source="recurring")
    reply = delete_text(session_factory, "Europe/Berlin", tx_id)
    assert "recurring.yml" in reply
    with session_factory() as session:
        assert session.get(Transaction, tx_id) is not None


def test_delete_text_unknown_id(session_factory):
    assert delete_text(session_factory, "Europe/Berlin", 999) == "No transaction #999."


def test_edit_text_amount(session_factory):
    tx_id = _add_expense(session_factory, amount_cents=3000)
    reply = edit_text(session_factory, "Europe/Berlin", tx_id, "amount", "35.50")
    assert reply.startswith(f"Updated #{tx_id}")
    assert "35,50 €" in reply
    with session_factory() as session:
        assert session.get(Transaction, tx_id).amount_cents == 3550


def test_edit_text_category(session_factory):
    tx_id = _add_expense(session_factory)
    reply = edit_text(session_factory, "Europe/Berlin", tx_id, "category", "Dining")
    assert "Dining" in reply
    with session_factory() as session:
        dining = session.query(Category).filter_by(name="Dining").one()
        assert session.get(Transaction, tx_id).category_id == dining.id


def test_edit_text_date(session_factory):
    tx_id = _add_expense(session_factory)
    reply = edit_text(session_factory, "Europe/Berlin", tx_id, "date", "2026-07-15")
    assert "2026-07-15" in reply
    with session_factory() as session:
        tx = session.get(Transaction, tx_id)
        assert tx.ts.month == 7
        assert tx.ts.day == 15


def test_edit_text_blocks_recurring(session_factory):
    tx_id = _add_expense(session_factory, source="recurring")
    reply = edit_text(session_factory, "Europe/Berlin", tx_id, "amount", "10")
    assert "recurring.yml" in reply


def test_edit_text_rejects_future_date(session_factory):
    tx_id = _add_expense(session_factory)
    reply = edit_text(session_factory, "Europe/Berlin", tx_id, "date", "2099-01-01")
    assert reply == "That date is in the future."
