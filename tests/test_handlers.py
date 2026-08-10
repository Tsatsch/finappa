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
