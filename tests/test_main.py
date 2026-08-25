import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from telegram.error import NetworkError

from app.config import Settings
from app.db import Category, RecurringRule, get_sessionmaker, init_db, make_engine
from app.handlers import BOT_COMMANDS, register_bot_commands
from app.main import _configure_logging, backup_job, build_application, error_handler, recurring_job


def test_configure_logging_suppresses_httpx_info_to_avoid_token_leak():
    # httpx logs full request URLs at INFO, and Telegram's Bot API embeds the
    # token in the URL path itself (no Authorization header) — without this,
    # every getMe/getUpdates/sendMessage call writes the live bot token
    # straight into persisted container logs.
    _configure_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-tests")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "finappa.db"))
    return Settings(_env_file=None)


def test_build_application_wires_bot_data_and_jobs(settings, tmp_path):
    application = build_application(settings, recurring_yaml_path=str(tmp_path / "missing.yml"))

    assert application.bot_data["timezone"] == "Europe/Berlin"
    assert application.bot_data["openai_model"] == "gpt-5.6-luna"
    assert "session_factory" in application.bot_data
    assert application.bot_data["pending_confirmations"] == {}
    assert application.bot_data["pending_edits"] == {}
    assert application.post_init is register_bot_commands

    job_names = {job.name for job in application.job_queue.jobs()}
    assert {
        "recurring_check",
        "recurring_check_startup",
        "missed_recurring_startup",
        "daily_backup",
        "backup_health",
        "backup_health_startup",
    } <= job_names


async def test_register_bot_commands_includes_recent_delete_edit():
    # Telegram's "/" menu is independent of CommandHandler registration and
    # HELP_TEXT — without setMyCommands covering every handler, new commands
    # stay usable but invisible in the client suggestion list (as happened
    # with /recent, /delete, /edit). ExtBot freezes attributes, so call the
    # post_init helper against a plain namespace instead of patching the bot.
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await register_bot_commands(SimpleNamespace(bot=bot))

    bot.set_my_commands.assert_called_once_with(BOT_COMMANDS)
    names = {cmd.command for cmd in BOT_COMMANDS}
    assert {
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
    } <= names


def test_scheduled_jobs_have_unlimited_misfire_grace_time(settings, tmp_path):
    # Regression test: APScheduler's default misfire_grace_time is 1 second.
    # run_once(..., when=0) targets "right now", so any startup delay past 1s
    # (normal — connecting to Telegram, etc.) causes the job to be silently
    # skipped entirely rather than run late. Observed in production: a
    # recurring_check_startup run missed by 2.54s and never executed, so
    # rent never got logged. All jobs here are idempotent and safe to run
    # whenever the process next gets to them, so none should have a grace
    # time that can cause a silent skip.
    application = build_application(settings, recurring_yaml_path=str(tmp_path / "missing.yml"))

    jobs_by_name = {job.name: job for job in application.job_queue.jobs()}
    for name in (
        "recurring_check",
        "recurring_check_startup",
        "missed_recurring_startup",
        "daily_backup",
        "backup_health",
        "backup_health_startup",
    ):
        # getattr with a sentinel: APScheduler only resolves the
        # misfire_grace_time default when its scheduler actually starts, not
        # at add_job() time, so an unfixed job has no such attribute at all
        # yet rather than reading as the buggy default of 1 — direct access
        # would raise AttributeError instead of giving a readable failure.
        grace = getattr(jobs_by_name[name].job, "misfire_grace_time", "UNSET")
        assert grace is None, (
            f"{name}.misfire_grace_time == {grace!r}, expected None (job_kwargs "
            "must set it explicitly) — a finite grace time can be silently skipped"
        )


def _add_rule(session, key="rent", day_of_month=1, amount_cents=120000, active=True, label="Rent"):
    category = session.query(Category).filter_by(name="Housing").one()
    rule = RecurringRule(
        key=key, label=label, amount_cents=amount_cents, type="EXPENSE",
        category_id=category.id, day_of_month=day_of_month, active=active,
    )
    session.add(rule)
    session.commit()
    return rule


async def test_recurring_job_sends_message_when_rule_fires(settings):
    # Exact-day match now (app/recurring.py) — pass a fixed `at` rather than
    # relying on the real day the test happens to run on.
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        _add_rule(session, day_of_month=1, amount_cents=120000, label="Rent")

    context = SimpleNamespace(
        bot_data={"session_factory": session_factory, "settings": settings},
        bot=AsyncMock(),
    )

    # Regression test: run_recurring_check commits internally, which (with
    # SQLAlchemy's default expire_on_commit=True) expires all attributes on
    # the RecurringRule objects it returns. Accessing rule.label/amount_cents
    # after the `with session_factory()` block has closed must not raise
    # DetachedInstanceError.
    at = datetime(2026, 3, 1, 6, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    await recurring_job(context, at=at)

    context.bot.send_message.assert_called_once()
    _, kwargs = context.bot.send_message.call_args
    assert kwargs["chat_id"] == settings.allowed_telegram_user_id
    assert "Rent" in kwargs["text"]
    assert "1.200,00" in kwargs["text"]


async def test_recurring_job_no_message_when_nothing_fires(settings):
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        _add_rule(session, day_of_month=1, active=False)

    context = SimpleNamespace(
        bot_data={"session_factory": session_factory, "settings": settings},
        bot=AsyncMock(),
    )

    await recurring_job(context)

    context.bot.send_message.assert_not_called()


async def test_backup_job_with_channel_sends_document_and_cleans_up(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-tests")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "finappa.db"))
    monkeypatch.setenv("BACKUP_CHANNEL_ID", "999")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    settings_with_backup = Settings(_env_file=None)

    engine = make_engine(settings_with_backup.db_path)
    init_db(engine)

    context = SimpleNamespace(
        bot_data={"settings": settings_with_backup},
        bot=AsyncMock(),
    )

    await backup_job(context)

    context.bot.send_document.assert_called_once()
    _, kwargs = context.bot.send_document.call_args
    assert kwargs["chat_id"] == 999
    assert kwargs["filename"].endswith(".db.enc")

    snapshot_dir = Path(settings_with_backup.db_path).parent
    leftover_enc_files = list(snapshot_dir.glob("*.enc"))
    assert leftover_enc_files == []


async def test_backup_job_without_channel_does_not_send_but_prunes(settings):
    engine = make_engine(settings.db_path)
    init_db(engine)

    snapshot_dir = Path(settings.db_path).parent
    for day in range(1, 11):
        (snapshot_dir / f"backup-2000-01-{day:02d}.db").write_text("fake")

    context = SimpleNamespace(
        bot_data={"settings": settings},
        bot=AsyncMock(),
    )

    await backup_job(context)

    context.bot.send_document.assert_not_called()
    remaining = sorted(p.name for p in snapshot_dir.glob("backup-*.db"))
    assert len(remaining) == 7


async def test_error_handler_swallows_network_error(caplog):
    settings = Settings(
        telegram_bot_token="t",
        allowed_telegram_user_id=1,
        openai_api_key="k",
        _env_file=None,
    )
    bot = AsyncMock()
    context = SimpleNamespace(
        error=NetworkError("drop"),
        bot_data={"settings": settings},
        bot=bot,
    )

    await error_handler(None, context)

    bot.send_message.assert_not_called()
    assert any("Telegram network error while processing update" in r.message for r in caplog.records)


async def test_error_handler_notifies_on_non_network_errors():
    settings = Settings(
        telegram_bot_token="t",
        allowed_telegram_user_id=1,
        openai_api_key="k",
        _env_file=None,
    )
    bot = AsyncMock()
    context = SimpleNamespace(
        error=RuntimeError("boom"),
        bot_data={"settings": settings},
        bot=bot,
    )

    await error_handler(None, context)

    bot.send_message.assert_called_once()
    assert "Something went wrong" in bot.send_message.call_args.kwargs["text"]
