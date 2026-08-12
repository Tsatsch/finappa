import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from app.db import Category, Merchant, Transaction, init_db, make_engine
from stats.app import create_app
from stats.data import fetch_ledger
from stats.settings import StatsSettings


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "finappa.db"
    engine = make_engine(str(path))
    init_db(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        groceries = session.query(Category).filter_by(name="Groceries").one()
        salary = session.query(Category).filter_by(name="Salary").one()
        merchant = Merchant(
            name="rewe",
            display_name="Rewe",
            category_id=groceries.id,
        )
        session.add(merchant)
        session.flush()
        session.add_all(
            [
                Transaction(
                    ts=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
                    amount_cents=3000,
                    type="EXPENSE",
                    merchant_id=merchant.id,
                    category_id=groceries.id,
                    notes=None,
                    source="message",
                    source_update_id=1,
                ),
                Transaction(
                    ts=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc),
                    amount_cents=350000,
                    type="INCOME",
                    merchant_id=None,
                    category_id=salary.id,
                    notes="July salary",
                    source="message",
                    source_update_id=2,
                ),
            ]
        )
        session.commit()
    return path


def test_fetch_ledger_returns_local_calendar_fields(db_path: Path):
    payload = fetch_ledger(db_path, "Europe/Berlin")
    assert payload["timezone"] == "Europe/Berlin"
    assert len(payload["transactions"]) == 2
    by_type = {t["type"]: t for t in payload["transactions"]}
    expense = by_type["EXPENSE"]
    assert expense["amount_cents"] == 3000
    assert expense["category"] == "Groceries"
    assert expense["merchant"] == "Rewe"
    assert expense["local_year"] == 2026
    assert expense["local_month"] == 8
    assert expense["local_date"] == "2026-08-05"


def test_fetch_ledger_is_read_only(db_path: Path):
    fetch_ledger(db_path, "Europe/Berlin")
    # Opening again still works; mode=ro must not require a write lock.
    again = fetch_ledger(db_path, "Europe/Berlin")
    assert len(again["transactions"]) == 2


@pytest.fixture
def client(db_path: Path) -> TestClient:
    settings = StatsSettings(
        db_path=str(db_path),
        timezone="Europe/Berlin",
        stats_username="ada",
        stats_password="hunter2",
        stats_host="127.0.0.1",
        stats_port=8080,
    )
    return TestClient(create_app(settings))


def test_api_requires_auth(client: TestClient):
    response = client.get("/api/data")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("basic")


def test_api_rejects_bad_password(client: TestClient):
    response = client.get("/api/data", headers=_auth_header("ada", "wrong"))
    assert response.status_code == 401


def test_api_returns_ledger_with_auth(client: TestClient):
    response = client.get("/api/data", headers=_auth_header("ada", "hunter2"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "Europe/Berlin"
    assert len(payload["transactions"]) == 2


def test_index_requires_auth_and_serves_html(client: TestClient):
    assert client.get("/").status_code == 401
    response = client.get("/", headers=_auth_header("ada", "hunter2"))
    assert response.status_code == 200
    assert "finappa" in response.text
    assert 'id="chart-category"' in response.text
    assert 'id="chart-merchant"' in response.text
    assert 'id="overview-table"' in response.text
