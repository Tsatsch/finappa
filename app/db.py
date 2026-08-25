from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.categories import CategoryName


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_update_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    recurring_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_rules.id"), nullable=True, index=True
    )


class RecurringRule(Base):
    __tablename__ = "recurring_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


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
    canonical_merchant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    absorbed_normalized: Mapped[str] = mapped_column(String, nullable=False)
    absorbed_display_name: Mapped[str] = mapped_column(String, nullable=False)
    absorbed_category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    alias_normalized: Mapped[str] = mapped_column(String, nullable=False)
    tx_ids_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of ints


def make_engine(db_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_wal(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    with engine.begin() as connection:
        foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list(cleanup_session_merges)"
        ).fetchall()
        if any(row[2] == "merchants" for row in foreign_keys):
            connection.exec_driver_sql("DROP TABLE cleanup_session_merges")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        existing = {c.name for c in session.query(Category).all()}
        for name in CategoryName:
            if name.value not in existing:
                session.add(Category(name=name.value))
        session.commit()


def get_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine)
