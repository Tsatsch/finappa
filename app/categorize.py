import difflib

from sqlalchemy.orm import Session

from app.categories import CategoryName
from app.db import Category, Merchant, Transaction

FUZZY_CUTOFF = 0.85


class RecatError(Exception):
    """User-facing error for /recat: unknown category or unknown merchant."""


def _normalize(name: str) -> str:
    return name.strip().lower()


def find_matching_merchant(session: Session, merchant_name: str) -> Merchant | None:
    if not merchant_name:
        return None
    normalized = _normalize(merchant_name)
    known = {m.name: m for m in session.query(Merchant).all()}
    match = difflib.get_close_matches(normalized, known.keys(), n=1, cutoff=FUZZY_CUTOFF)
    return known[match[0]] if match else None


def resolve_category(
    session: Session, merchant_name: str, llm_category: CategoryName
) -> tuple[Merchant | None, Category]:
    if not merchant_name:
        category = session.query(Category).filter_by(name=llm_category.value).one()
        return None, category

    existing = find_matching_merchant(session, merchant_name)
    if existing:
        category = session.get(Category, existing.category_id)
        return existing, category

    category = session.query(Category).filter_by(name=llm_category.value).one()
    merchant = Merchant(name=_normalize(merchant_name), display_name=merchant_name, category_id=category.id)
    session.add(merchant)
    session.flush()
    return merchant, category


def recat_merchant(session: Session, merchant_name: str, category_name: str) -> tuple[Merchant, int]:
    try:
        category_enum = CategoryName(category_name)
    except ValueError:
        valid = ", ".join(c.value for c in CategoryName)
        raise RecatError(f"Unknown category '{category_name}'. Valid categories: {valid}")

    merchant = find_matching_merchant(session, merchant_name)
    if merchant is None:
        raise RecatError(f"No merchant matching '{merchant_name}' found.")

    category = session.query(Category).filter_by(name=category_enum.value).one()
    merchant.category_id = category.id

    count = (
        session.query(Transaction)
        .filter_by(merchant_id=merchant.id)
        .update({"category_id": category.id}, synchronize_session="fetch")
    )
    session.commit()
    return merchant, count
