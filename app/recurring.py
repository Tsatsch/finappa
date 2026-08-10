from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy.orm import Session

from app.analytics import month_bounds_utc
from app.categories import CategoryName
from app.db import Category, RecurringRule, Transaction


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
        if rule.day_of_month > now_local.day:
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
