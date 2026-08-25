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


def test_init_db_removes_legacy_canonical_merchant_fk(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE cleanup_session_merges (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES cleanup_sessions(id),
                    canonical_merchant_id INTEGER NOT NULL REFERENCES merchants(id),
                    absorbed_normalized VARCHAR NOT NULL,
                    absorbed_display_name VARCHAR NOT NULL,
                    absorbed_category_id INTEGER NOT NULL REFERENCES categories(id),
                    alias_normalized VARCHAR NOT NULL,
                    tx_ids_json TEXT NOT NULL
                )
                """
            )
        )

    init_db(engine)

    with engine.connect() as conn:
        foreign_key_targets = {
            row[2]
            for row in conn.execute(
                text("PRAGMA foreign_key_list(cleanup_session_merges)")
            )
        }
    assert "merchants" not in foreign_key_targets
    assert "cleanup_sessions" in foreign_key_targets


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
