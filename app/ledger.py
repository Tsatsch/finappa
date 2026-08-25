from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from app.analytics import format_cents
from app.categories import CategoryName
from app.db import Category, Merchant, Transaction
from app.extractor import MAX_AMOUNT

EDIT_USAGE = "Usage: /edit <id> amount <eur> | category <name> | date <YYYY-MM-DD>"
RECENT_MAX = 50
RECENT_DEFAULT = 10
FIND_USAGE = "Usage: /find <query>"
FIND_MAX = 20


@dataclass
class PendingCategoryEdit:
    pending_id: str
    tx_id: int
    category_name: str
    merchant_id: int
    merchant_display: str


@dataclass
class EditOutcome:
    reply: str
    pending: PendingCategoryEdit | None = None


def local_date_for_ts(ts: datetime, tz_name: str) -> date:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ZoneInfo(tz_name)).date()


def ts_from_local_date(local_date: date, tz_name: str) -> datetime:
    local_noon = datetime(
        local_date.year, local_date.month, local_date.day, 12, 0, tzinfo=ZoneInfo(tz_name)
    )
    return local_noon.astimezone(timezone.utc)


def resolve_transaction_ts(
    local_date: date | None, tz_name: str, now: datetime | None = None
) -> datetime:
    if local_date is not None:
        return ts_from_local_date(local_date, tz_name)
    return now or datetime.now(timezone.utc)


def today_local(tz_name: str, at: datetime | None = None) -> date:
    ts = at or datetime.now(timezone.utc)
    return local_date_for_ts(ts, tz_name)


def format_transaction_line(session: Session, tx: Transaction, tz_name: str) -> str:
    category = session.get(Category, tx.category_id)
    merchant = session.get(Merchant, tx.merchant_id) if tx.merchant_id else None
    label = merchant.display_name if merchant else (tx.notes or tx.type.title())
    local = local_date_for_ts(tx.ts, tz_name)
    line = f"#{tx.id} · {format_cents(tx.amount_cents)} · {category.name} · {label} · {local.isoformat()}"
    if tx.source == "recurring":
        line += " · recurring"
    return line


def _get_transaction(session: Session, tx_id: int) -> Transaction | None:
    return session.get(Transaction, tx_id)


def _recurring_block_message(tx_id: int) -> str:
    return f"#{tx_id} is a recurring entry — edit recurring.yml instead."


def recent_text(session_factory, tz_name: str, limit: int = RECENT_DEFAULT) -> str:
    limit = min(max(1, limit), RECENT_MAX)
    with session_factory() as session:
        rows = (
            session.query(Transaction)
            .order_by(Transaction.id.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            return "No transactions yet."
        return "\n".join(format_transaction_line(session, tx, tz_name) for tx in rows)


def find_text(session_factory, tz_name: str, query: str) -> str:
    q = query.strip()
    if not q:
        return FIND_USAGE
    pattern = f"%{q}%"
    with session_factory() as session:
        rows = (
            session.query(Transaction)
            .outerjoin(Merchant, Transaction.merchant_id == Merchant.id)
            .filter(
                or_(
                    Merchant.display_name.ilike(pattern),
                    Transaction.notes.ilike(pattern),
                )
            )
            .order_by(Transaction.id.desc())
            .limit(FIND_MAX)
            .all()
        )
        if not rows:
            return f"No matches for '{q}'."
        return "\n".join(format_transaction_line(session, tx, tz_name) for tx in rows)


def delete_text(session_factory, tz_name: str, tx_id: int) -> str:
    with session_factory() as session:
        tx = _get_transaction(session, tx_id)
        if tx is None:
            return f"No transaction #{tx_id}."
        if tx.source == "recurring":
            return _recurring_block_message(tx_id)
        line = format_transaction_line(session, tx, tz_name)
        session.delete(tx)
        session.commit()
    return f"Deleted {line}."


def _parse_amount_eur(value: str) -> int:
    amount = float(value.replace(",", "."))
    if amount <= 0 or amount > MAX_AMOUNT:
        raise ValueError("amount out of range")
    return round(amount * 100)


def _parse_local_date(value: str, tz_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("bad date format") from exc
    if parsed > today_local(tz_name):
        raise ValueError("future date")
    return parsed


def edit_text(session_factory, tz_name: str, tx_id: int, field: str, value: str) -> EditOutcome:
    field = field.lower()
    if field not in {"amount", "category", "date"}:
        return EditOutcome(EDIT_USAGE)

    with session_factory() as session:
        tx = _get_transaction(session, tx_id)
        if tx is None:
            return EditOutcome(f"No transaction #{tx_id}.")
        if tx.source == "recurring":
            return EditOutcome(_recurring_block_message(tx_id))

        try:
            if field == "amount":
                tx.amount_cents = _parse_amount_eur(value)
            elif field == "category":
                try:
                    category_enum = CategoryName(value)
                except ValueError:
                    valid = ", ".join(c.value for c in CategoryName)
                    return EditOutcome(
                        f"Unknown category '{value}'. Valid categories: {valid}"
                    )
                if tx.merchant_id is not None:
                    merchant = session.get(Merchant, tx.merchant_id)
                    pending = PendingCategoryEdit(
                        pending_id="",  # filled by handler
                        tx_id=tx_id,
                        category_name=category_enum.value,
                        merchant_id=tx.merchant_id,
                        merchant_display=merchant.display_name if merchant else "merchant",
                    )
                    return EditOutcome(
                        reply=(
                            f"Change #{tx_id} ({pending.merchant_display}) → "
                            f"{category_enum.value}?\n"
                            "This row only, or update merchant memory and all past "
                            "transactions for this merchant?"
                        ),
                        pending=pending,
                    )
                category = session.query(Category).filter_by(name=category_enum.value).one()
                tx.category_id = category.id
            else:
                local = _parse_local_date(value, tz_name)
                tx.ts = ts_from_local_date(local, tz_name)
        except ValueError as exc:
            if str(exc) == "amount out of range":
                return EditOutcome(
                    f"Nothing changed — an amount of {value} doesn't look right.\n"
                    f"{EDIT_USAGE}"
                )
            if str(exc) == "future date":
                return EditOutcome("That date is in the future.")
            return EditOutcome("Use YYYY-MM-DD, e.g. 2026-08-10")

        session.commit()
        session.refresh(tx)
        line = format_transaction_line(session, tx, tz_name)
    return EditOutcome(f"Updated {line}")


def apply_category_edit_row(session_factory, tz_name: str, tx_id: int, category_name: str) -> str:
    with session_factory() as session:
        tx = _get_transaction(session, tx_id)
        if tx is None:
            return f"No transaction #{tx_id}."
        if tx.source == "recurring":
            return _recurring_block_message(tx_id)
        category = session.query(Category).filter_by(name=category_name).one()
        tx.category_id = category.id
        session.commit()
        session.refresh(tx)
        line = format_transaction_line(session, tx, tz_name)
    return f"Updated {line}"


def apply_category_edit_merchant(
    session_factory, tz_name: str, tx_id: int, category_name: str, merchant_id: int
) -> str:
    with session_factory() as session:
        tx = _get_transaction(session, tx_id)
        if tx is None:
            return f"No transaction #{tx_id}."
        if tx.source == "recurring":
            return _recurring_block_message(tx_id)
        merchant = session.get(Merchant, merchant_id)
        if merchant is None:
            return f"No merchant for #{tx_id}."
        category = session.query(Category).filter_by(name=category_name).one()
        merchant.category_id = category.id
        count = (
            session.query(Transaction)
            .filter_by(merchant_id=merchant.id)
            .update({"category_id": category.id}, synchronize_session="fetch")
        )
        session.commit()
        session.refresh(tx)
        line = format_transaction_line(session, tx, tz_name)
    return (
        f"Updated {line}\n"
        f"{merchant.display_name} → {category_name}, {count} past transaction(s) updated."
    )


def logged_item_line(
    amount_cents: int, label: str, category_name: str, ts: datetime, tz_name: str
) -> str:
    line = f"• {format_cents(amount_cents)} · {label} · {category_name}"
    local = local_date_for_ts(ts, tz_name)
    if local != today_local(tz_name):
        line += f" · {local.isoformat()}"
    return line
