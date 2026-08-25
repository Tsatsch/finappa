import csv
import io
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.analytics import monthly_summary
from app.categories import CategoryName
from app.categorize import resolve_category
from app.db import Category, Merchant, Transaction, get_sessionmaker, init_db, make_engine
from app.extractor import ExtractionResult, TransactionItem
from app.handlers import (
    PendingConfirmation,
    confirm_category,
    export_csv,
    handle_category_confirmation,
    process_message,
    recat_text,
    summary_text,
)
from app.ledger import ts_from_local_date


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    return get_sessionmaker(engine)


def _stub_client(*items: TransactionItem) -> MagicMock:
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [
        MagicMock(message=MagicMock(parsed=ExtractionResult(transactions=list(items))))
    ]
    client.chat.completions.parse.return_value = completion
    return client


def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0, currency="EUR", merchant="Rewe",
        category=CategoryName.GROCERIES, category_confident=True,
        type="EXPENSE", notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)


def test_process_message_logs_and_replies(session_factory):
    client = _stub_client(_item())
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=42
    )

    assert "Logged 1" in result.reply_text
    assert "Rewe" in result.reply_text
    assert result.pending == []
    with session_factory() as session:
        txn = session.query(Transaction).one()
        assert txn.amount_cents == 3000
        assert txn.source_update_id == 42


def test_process_message_rejection_logs_nothing(session_factory):
    client = _stub_client(_item(currency="USD"))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30 USD Rewe", update_id=1
    )

    assert "EUR" in result.reply_text
    with session_factory() as session:
        assert session.query(Transaction).count() == 0


def test_process_message_multi_item_rejection_logs_nothing(session_factory):
    # AGENTS.md invariant: one invalid item in a multi-item message rejects the
    # whole message. Regression guard for the case a single-item test can't
    # cover — the valid item must not get logged just because it came first.
    client = _stub_client(_item(), _item(currency="USD", merchant="Shell"))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe and 15 USD Shell", update_id=1
    )

    assert "EUR" in result.reply_text
    with session_factory() as session:
        assert session.query(Transaction).count() == 0


def test_process_message_holds_uncertain_new_merchant(session_factory):
    client = _stub_client(_item(merchant="Obscure Shop LLC", category_confident=False))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ at that place", update_id=1
    )

    assert result.reply_text is None
    assert len(result.pending) == 1
    pending = result.pending[0]
    assert pending.amount_cents == 3000
    assert pending.merchant == "Obscure Shop LLC"
    assert pending.type == "EXPENSE"
    with session_factory() as session:
        assert session.query(Transaction).count() == 0
        assert session.query(Merchant).count() == 0


def test_process_message_mixed_confident_and_uncertain(session_factory):
    client = _stub_client(
        _item(merchant="Rewe", category_confident=True),
        _item(merchant="Obscure Shop LLC", category_confident=False, amount=15.0),
    )
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe and 15€ somewhere", update_id=1
    )

    assert "Logged 1" in result.reply_text
    assert "Rewe" in result.reply_text
    assert len(result.pending) == 1
    assert result.pending[0].merchant == "Obscure Shop LLC"
    with session_factory() as session:
        assert session.query(Transaction).count() == 1


def test_process_message_known_merchant_ignores_low_confidence(session_factory):
    client = _stub_client(_item(merchant="Rewe", category_confident=True))
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    client2 = _stub_client(
        _item(merchant="Rewe", category_confident=False, category=CategoryName.DINING)
    )
    result = process_message(
        session_factory, client2, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe again", update_id=2
    )

    assert result.reply_text is not None
    assert result.pending == []
    with session_factory() as session:
        assert session.query(Transaction).count() == 2


def test_summary_text_reports_zero_on_empty_ledger(session_factory):
    text = summary_text(session_factory, "Europe/Berlin")
    assert "0,00 €" in text


def test_summary_text_for_past_month(session_factory):
    july = ts_from_local_date(date(2026, 7, 10), "Europe/Berlin")
    with session_factory() as session:
        groceries = session.query(Category).filter_by(name="Groceries").one()
        session.add(
            Transaction(
                ts=july,
                amount_cents=4200,
                type="EXPENSE",
                category_id=groceries.id,
                notes="past",
                source="message",
                source_update_id=1,
            )
        )
        session.commit()
    text = summary_text(session_factory, "Europe/Berlin", month="2026-07")
    assert text.startswith("Summary for 2026-07:")
    assert "42,00" in text


def test_summary_text_rejects_future_month(session_factory):
    assert summary_text(session_factory, "Europe/Berlin", month="2099-01") == (
        "That month is in the future."
    )


def test_summary_text_bad_format(session_factory):
    assert summary_text(session_factory, "Europe/Berlin", month="2026-13") == (
        "Usage: /summary [YYYY-MM]"
    )


def test_export_csv_contains_logged_transaction(session_factory):
    client = _stub_client(_item())
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    buffer = export_csv(session_factory)
    rows = list(csv.reader(io.TextIOWrapper(buffer, encoding="utf-8")))
    assert rows[0] == ["timestamp", "amount_eur", "type", "category", "merchant", "notes", "source"]
    assert rows[1][1] == "30.00"
    assert rows[1][3] == "Groceries"
    assert rows[1][4] == "Rewe"


def test_recat_text_updates_and_reports_count(session_factory):
    client = _stub_client(_item())
    process_message(session_factory, client, "gpt-4o-mini", "Europe/Berlin", "30€ Rewe", update_id=1)

    reply = recat_text(session_factory, "Rewe", "Dining")

    assert "1 past transaction" in reply
    with session_factory() as session:
        dining = session.query(Category).filter_by(name="Dining").one()
        txn = session.query(Transaction).one()
        assert txn.category_id == dining.id


def test_recat_text_unknown_merchant_reports_error(session_factory):
    reply = recat_text(session_factory, "Nonexistent", "Dining")
    assert "No merchant" in reply


def test_confirm_category_inserts_and_reports(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    reply = confirm_category(session_factory, pending, "Shopping", "Europe/Berlin", update_id=99)

    assert "15,00" in reply
    assert "Shopping" in reply
    with session_factory() as session:
        shopping = session.query(Category).filter_by(name="Shopping").one()
        txn = session.query(Transaction).one()
        assert txn.amount_cents == 1500
        assert txn.category_id == shopping.id
        assert txn.source_update_id == 99


def test_confirm_category_uses_pending_ts_not_now(session_factory):
    original_ts = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    pending = PendingConfirmation(
        pending_id="abc123", ts=original_ts, amount_cents=1000,
        type="EXPENSE", merchant="Shop", notes=None,
    )
    confirm_category(session_factory, pending, "Shopping", "Europe/Berlin", update_id=1)
    with session_factory() as session:
        txn = session.query(Transaction).one()
        # Transaction.ts is a plain (non-timezone-aware) DateTime column; SQLite
        # always strips tzinfo on read-back regardless, so reattach UTC before
        # comparing — this is a pre-existing characteristic of the schema, not
        # something confirm_category controls.
        assert txn.ts.replace(tzinfo=timezone.utc) == original_ts


def test_confirm_category_reports_stored_category_not_tapped_choice(session_factory):
    # Regression guard: resolve_category's job is "stored category always
    # wins over a new guess." If the merchant already has a stored category
    # by confirmation time (e.g. a confident item earlier in the same
    # message created it, or a second pending confirmation for the same
    # merchant was answered first), the Transaction gets the STORED
    # category — but the reply text used to interpolate the raw
    # category_name the user tapped instead, silently lying about what was
    # logged whenever the two differ.
    with session_factory() as session:
        resolve_category(session, "Kiosk 24", CategoryName.GROCERIES)
        session.commit()

    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=500,
        type="EXPENSE", merchant="Kiosk 24", notes=None,
    )
    # Tap a DIFFERENT category than what's already stored for this merchant.
    reply = confirm_category(session_factory, pending, "Dining", "Europe/Berlin", update_id=1)

    assert "Groceries" in reply
    assert "Dining" not in reply
    with session_factory() as session:
        groceries = session.query(Category).filter_by(name="Groceries").one()
        txn = session.query(Transaction).one()
        assert txn.category_id == groceries.id


def _fake_callback_context(session_factory, pending_map, allowed_user_id=12345):
    settings = SimpleNamespace(allowed_telegram_user_id=allowed_user_id)
    return SimpleNamespace(
        bot_data={
            "session_factory": session_factory,
            "pending_confirmations": pending_map,
            "timezone": "Europe/Berlin",
            "settings": settings,
        }
    )


async def test_handle_category_confirmation_inserts_on_valid_choice(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:abc123:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=999)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    reply_text = query.edit_message_text.call_args[0][0]
    assert "Shopping" in reply_text
    assert "abc123" not in context.bot_data["pending_confirmations"]
    with session_factory() as session:
        assert session.query(Transaction).count() == 1


async def test_handle_category_confirmation_skip_discards_without_logging(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:abc123:SKIP",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1000)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    assert "Discarded" in query.edit_message_text.call_args[0][0]
    with session_factory() as session:
        assert session.query(Transaction).count() == 0


async def test_handle_category_confirmation_expired_pending_id(session_factory):
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), data="pconf:doesnotexist:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1001)
    context = _fake_callback_context(session_factory, {})

    await handle_category_confirmation(update, context)

    assert "expired" in query.edit_message_text.call_args[0][0]


async def test_handle_category_confirmation_ignores_wrong_user(session_factory):
    pending = PendingConfirmation(
        pending_id="abc123", ts=datetime.now(timezone.utc), amount_cents=1500,
        type="EXPENSE", merchant="Obscure Shop", notes=None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99999), data="pconf:abc123:Shopping",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, update_id=1002)
    context = _fake_callback_context(session_factory, {"abc123": pending})

    await handle_category_confirmation(update, context)

    query.answer.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    assert "abc123" in context.bot_data["pending_confirmations"]


async def test_register_bot_commands_sets_telegram_menu():
    from app.handlers import BOT_COMMANDS, register_bot_commands

    application = SimpleNamespace(bot=AsyncMock())
    await register_bot_commands(application)

    application.bot.set_my_commands.assert_awaited_once_with(BOT_COMMANDS)
    command_names = {cmd.command for cmd in BOT_COMMANDS}
    assert command_names == {
        "start",
        "help",
        "summary",
        "recent",
        "find",
        "delete",
        "edit",
        "undo",
        "export",
        "recat",
        "backup_status",
    }


def test_process_message_backdated_item_lands_in_correct_month(session_factory):
    client = _stub_client(_item(local_date=date(2026, 7, 15)))
    result = process_message(
        session_factory, client, "gpt-4o-mini", "Europe/Berlin",
        "on July 15 I spent 30 at Rewe", update_id=1,
    )
    assert "2026-07-15" in result.reply_text
    with session_factory() as session:
        txn = session.query(Transaction).one()
        expected = ts_from_local_date(date(2026, 7, 15), "Europe/Berlin")
        assert txn.ts.replace(tzinfo=timezone.utc) == expected

    july = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    august = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        assert monthly_summary(session, "Europe/Berlin", at=july)["expense_cents"] == 3000
        assert monthly_summary(session, "Europe/Berlin", at=august)["expense_cents"] == 0
