from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_LEDGER_SQL = """
SELECT
    t.id,
    t.ts,
    t.amount_cents,
    t.type,
    t.notes,
    t.source,
    c.name AS category,
    m.display_name AS merchant
FROM transactions AS t
JOIN categories AS c ON c.id = t.category_id
LEFT JOIN merchants AS m ON m.id = t.merchant_id
ORDER BY t.ts ASC, t.id ASC
"""


def _parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        # SQLite may return "YYYY-MM-DD HH:MM:SS[.fff]" without a timezone.
        text = value.replace("T", " ")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def fetch_ledger(db_path: str | Path, tz_name: str) -> dict:
    """Return the full ledger as JSON-serializable dict. Read-only."""
    path = Path(db_path).resolve()
    uri = path.as_uri() + "?mode=ro"
    tz = ZoneInfo(tz_name)
    generated_at = datetime.now(timezone.utc)

    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_LEDGER_SQL).fetchall()
    finally:
        conn.close()

    transactions: list[dict] = []
    for row in rows:
        ts = _parse_ts(row["ts"])
        local = ts.astimezone(tz)
        transactions.append(
            {
                "id": int(row["id"]),
                "ts": ts.isoformat(),
                "local_date": local.date().isoformat(),
                "local_year": local.year,
                "local_month": local.month,
                "amount_cents": int(row["amount_cents"]),
                "type": row["type"],
                "category": row["category"],
                "merchant": row["merchant"],
                "notes": row["notes"],
                "source": row["source"],
            }
        )

    return {
        "timezone": tz_name,
        "generated_at": generated_at.isoformat(),
        "transactions": transactions,
    }
