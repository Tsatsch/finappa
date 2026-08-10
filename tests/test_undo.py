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
