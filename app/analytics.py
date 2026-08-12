from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import Category, Transaction


def month_bounds_utc(tz_name: str, at: datetime | None = None) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    now_local = (at or datetime.now(timezone.utc)).astimezone(tz)
    start_local = datetime(now_local.year, now_local.month, 1, tzinfo=tz)
    if now_local.month == 12:
        next_start_local = datetime(now_local.year + 1, 1, 1, tzinfo=tz)
    else:
        next_start_local = datetime(now_local.year, now_local.month + 1, 1, tzinfo=tz)
    return start_local.astimezone(timezone.utc), next_start_local.astimezone(timezone.utc)


def format_cents(cents: int) -> str:
    sign = "−" if cents < 0 else ""
    formatted = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}{formatted} €"


def monthly_summary(session: Session, tz_name: str, at: datetime | None = None) -> dict:
    start_utc, end_utc = month_bounds_utc(tz_name, at)

    rows = (
        session.query(Transaction.type, Category.name, func.sum(Transaction.amount_cents))
        .join(Category, Transaction.category_id == Category.id)
        .filter(Transaction.ts >= start_utc, Transaction.ts < end_utc)
        .group_by(Transaction.type, Category.name)
        .all()
    )

    income_cents = 0
    expense_cents = 0
    by_category: dict[str, int] = {}

    for type_, category_name, total in rows:
        total = int(total or 0)
        if type_ == "INCOME":
            income_cents += total
        elif type_ == "EXPENSE":
            expense_cents += total
            by_category[category_name] = by_category.get(category_name, 0) + total
        else:
            raise ValueError(f"Unknown transaction type: {type_}")

    return {
        "income_cents": income_cents,
        "expense_cents": expense_cents,
        "net_cents": income_cents - expense_cents,
        "by_category": by_category,
    }


def format_summary_message(summary: dict) -> str:
    lines = [
        f"Income: {format_cents(summary['income_cents'])}",
        f"Expenses: {format_cents(summary['expense_cents'])}",
        f"Net: {format_cents(summary['net_cents'])}",
    ]
    if summary["by_category"]:
        lines.append("")
        lines.append("By category:")
        for name, cents in sorted(summary["by_category"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {format_cents(cents)}")
    return "\n".join(lines)
