import difflib

from sqlalchemy.orm import Session

from app.categories import CategoryName
from app.db import Category, Merchant, Transaction

FUZZY_CUTOFF = 0.85


class RecatError(Exception):
    """User-facing error for /recat: unknown category or unknown merchant."""


def _normalize(name: str) -> str:
    return name.strip().lower()


def resolve_category(
    session: Session, merchant_name: str, llm_category: CategoryName
) -> tuple[Merchant | None, Category]:
    if not merchant_name:
        category = session.query(Category).filter_by(name=llm_category.value).one()
        return None, category

    normalized = _normalize(merchant_name)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    if match:
        merchant = known[match[0]]
        category = session.get(Category, merchant.category_id)
        return merchant, category

    category = session.query(Category).filter_by(name=llm_category.value).one()
    merchant = Merchant(name=normalized, display_name=merchant_name, category_id=category.id)
    session.add(merchant)
    session.flush()
    return merchant, category


def recat_merchant(session: Session, merchant_name: str, category_name: str) -> tuple[Merchant, int]:
    try:
        category_enum = CategoryName(category_name)
    except ValueError:
        valid = ", ".join(c.value for c in CategoryName)
        raise RecatError(f"Unknown category '{category_name}'. Valid categories: {valid}")

    normalized = _normalize(merchant_name)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    if not match:
        raise RecatError(f"No merchant matching '{merchant_name}' found.")
    merchant = known[match[0]]

    category = session.query(Category).filter_by(name=category_enum.value).one()
    merchant.category_id = category.id

    count = (
        session.query(Transaction)
        .filter_by(merchant_id=merchant.id)
        .update({"category_id": category.id}, synchronize_session="fetch")
    )
    session.commit()
    return merchant, count
