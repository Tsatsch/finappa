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
