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

    # Typo (doubled letter); the LLM guesses wrong this time.
    # Verified: difflib.SequenceMatcher(None, "rewe", "rewwe").ratio() = 0.8889 > 0.85
    merchant, category = resolve_category(session, "Rewwe", CategoryName.DINING)
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
