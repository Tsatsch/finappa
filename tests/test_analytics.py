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
