from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy.orm import Session

from app.analytics import month_bounds_utc
from app.categories import CategoryName
from app.db import Category, Merchant, RecurringRule, Transaction

VALID_TYPES = {"EXPENSE", "INCOME"}


def load_recurring_rules(session: Session, yaml_path: str) -> None:
    path = Path(yaml_path)
    if not path.exists():
        return
    entries = yaml.safe_load(path.read_text()) or []
    for entry in entries:
        day_of_month = entry["day_of_month"]
        if not 1 <= day_of_month <= 28:
            raise ValueError(
                f"recurring.yml: '{entry['key']}' day_of_month must be 1..28, got {day_of_month}"
            )
        if entry["type"] not in VALID_TYPES:
            raise ValueError(
                f"recurring.yml: '{entry['key']}' type must be one of {sorted(VALID_TYPES)}, "
                f"got {entry['type']!r}"
            )
        category = session.query(Category).filter_by(name=CategoryName(entry["category"]).value).one()
        amount_cents = round(entry["amount"] * 100)
        rule = session.query(RecurringRule).filter_by(key=entry["key"]).one_or_none()
        if rule is None:
            rule = RecurringRule(key=entry["key"], category_id=category.id)
            session.add(rule)
        rule.label = entry["label"]
        rule.amount_cents = amount_cents
        rule.type = entry["type"]
        rule.category_id = category.id
        rule.day_of_month = day_of_month
        rule.active = entry.get("active", True)
    session.commit()


def run_recurring_check(
    session: Session, tz_name: str, at: datetime | None = None
) -> list[RecurringRule]:
    now_utc = at or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    start_utc, end_utc = month_bounds_utc(tz_name, at=now_utc)

    fired = []
    for rule in session.query(RecurringRule).filter_by(active=True).all():
        # Exact-day match only, by explicit request (2026-08-11) — a rule
        # does NOT catch up if its day already passed this month; it waits
        # for next month instead. Trade-off accepted: if the container is
        # down specifically on a rule's day, that month's entry is skipped
        # entirely rather than self-healing on a later check.
        if rule.day_of_month != now_local.day:
            continue
        already_logged = (
            session.query(Transaction)
            .filter(
                Transaction.recurring_rule_id == rule.id,
                Transaction.ts >= start_utc,
                Transaction.ts < end_utc,
            )
            .first()
        )
        if already_logged is not None:
            continue
        session.add(
            Transaction(
                ts=now_utc,
                amount_cents=rule.amount_cents,
                type=rule.type,
                category_id=rule.category_id,
                notes=rule.label,
                source="recurring",
                recurring_rule_id=rule.id,
            )
        )
        fired.append(rule)
    session.commit()
    return fired


def missed_recurring_rules(
    session: Session, tz_name: str, at: datetime | None = None
) -> list[RecurringRule]:
    """Active rules whose day already passed this month with no ledger row.

    Notify-only — never inserts. Exact-day fire remains in run_recurring_check.

    A month counts as logged if either:
    - a transaction has this ``recurring_rule_id``, or
    - a manual row matches the rule's label + amount (notes or merchant
      display_name), so free-text catch-up logs silence the alert.
    """
    now_utc = at or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    start_utc, end_utc = month_bounds_utc(tz_name, at=now_utc)
    missed: list[RecurringRule] = []
    for rule in session.query(RecurringRule).filter_by(active=True).all():
        if rule.day_of_month >= now_local.day:
            continue
        already_logged = (
            session.query(Transaction)
            .filter(
                Transaction.recurring_rule_id == rule.id,
                Transaction.ts >= start_utc,
                Transaction.ts < end_utc,
            )
            .first()
        )
        if already_logged is None:
            already_logged = (
                session.query(Transaction)
                .outerjoin(Merchant, Transaction.merchant_id == Merchant.id)
                .filter(
                    Transaction.amount_cents == rule.amount_cents,
                    Transaction.ts >= start_utc,
                    Transaction.ts < end_utc,
                    (Transaction.notes == rule.label)
                    | (Merchant.display_name == rule.label),
                )
                .first()
            )
        if already_logged is None:
            missed.append(rule)
    return missed


def format_missed_recurring_message(rules: list[RecurringRule]) -> str:
    lines = [
        "Recurring skipped this month (bot was down on their day — log manually if needed):"
    ]
    for rule in rules:
        lines.append(f"• {rule.label} (day {rule.day_of_month})")
    return "\n".join(lines)
