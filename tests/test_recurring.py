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
    at = datetime(2026, 3, 1, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

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


def test_day_already_passed_does_not_fire(session):
    # By explicit request (2026-08-11): a rule waits for its exact day next
    # month rather than catching up on a later check within the same month.
    # Superseded design: this used to self-heal (see git history) — the
    # user preferred no "surprise backdated-feeling" entries over that
    # resilience, after being shown the trade-off.
    _add_rule(session, day_of_month=1)
    at = datetime(2026, 3, 5, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    fired = run_recurring_check(session, "Europe/Berlin", at=at)

    assert fired == []
    assert session.query(Transaction).filter_by(source="recurring").count() == 0


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
