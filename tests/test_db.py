import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.categories import CategoryName
from app.db import Category, Transaction, init_db, make_engine, get_sessionmaker


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


def test_foreign_key_enforcement_enabled(tmp_path):
    from datetime import datetime, timezone

    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)

    # Attempt to insert a Transaction with a nonexistent category_id
    with pytest.raises(IntegrityError):
        with Session() as session:
            invalid_transaction = Transaction(
                ts=datetime.now(timezone.utc),
                amount_cents=1000,
                type="expense",
                merchant_id=None,
                category_id=9999,  # nonexistent category
                notes="test",
                source="test",
                source_update_id=None,
                recurring_rule_id=None
            )
            session.add(invalid_transaction)
            session.flush()  # FK check happens on flush


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
