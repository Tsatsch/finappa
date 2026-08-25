import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI
from telegram.error import NetworkError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from app.telegram_io import telegram_send

from app.analytics import format_cents
from app.backup import create_snapshot, encrypt_file, prune_snapshots, stale_backup_message
from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db, make_engine
from app.handlers import register_bot_commands, register_handlers
from app.recurring import load_recurring_rules, missed_recurring_rules, run_recurring_check

logger = logging.getLogger(__name__)


def _local_time(hour: int, minute: int, tz_name: str) -> time:
    return time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz_name))


async def recurring_job(context: ContextTypes.DEFAULT_TYPE, at: datetime | None = None) -> None:
    # `at` is test-only: APScheduler always calls this with just `context`,
    # so the default (real "now") is what runs in production. Mirrors
    # run_recurring_check's own `at` param so tests can pick a fixed date
    # instead of depending on which day they happen to run.
    session_factory = context.bot_data["session_factory"]
    settings: Settings = context.bot_data["settings"]
    with session_factory() as session:
        fired = run_recurring_check(session, settings.timezone, at=at)
        fired_values = [(rule.label, rule.amount_cents) for rule in fired]
    for label, amount_cents in fired_values:
        await telegram_send(
            lambda label=label, amount_cents=amount_cents: context.bot.send_message(
                chat_id=settings.allowed_telegram_user_id,
                text=f"Recurring: logged {label} ({format_cents(amount_cents)}).",
            ),
            ledger_written=True,
            context=f"recurring {label}",
        )


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    snapshot_dir = str(Path(settings.db_path).parent)
    try:
        snapshot_path = create_snapshot(settings.db_path, snapshot_dir)

        if settings.backup_channel_id is not None:
            encrypted_path = encrypt_file(snapshot_path, settings.backup_encryption_key)
            try:
                with open(encrypted_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=settings.backup_channel_id,
                        document=f,
                        filename=encrypted_path.name,
                        caption=f"finappa backup {snapshot_path.stem.removeprefix('backup-')}",
                    )
            finally:
                encrypted_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.exception("Backup job failed")
        await context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=f"Backup failed: {exc}",
        )
    finally:
        # Always prune, even on failure — the local snapshot from
        # create_snapshot (if it succeeded) should still count toward
        # pruning even when the remote send failed; don't lose it or let
        # snapshots accumulate forever under a persistent failure.
        prune_snapshots(snapshot_dir, keep=7)


async def backup_health_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    snapshot_dir = str(Path(settings.db_path).parent)
    message = stale_backup_message(snapshot_dir, tz_name=settings.timezone)
    if message is None:
        return
    await telegram_send(
        lambda: context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=message,
        ),
        context="backup health",
    )


async def missed_recurring_job(
    context: ContextTypes.DEFAULT_TYPE, at: datetime | None = None
) -> None:
    session_factory = context.bot_data["session_factory"]
    settings: Settings = context.bot_data["settings"]
    with session_factory() as session:
        missed = missed_recurring_rules(session, settings.timezone, at=at)
        payload = [(rule.label, rule.day_of_month) for rule in missed]
    if not payload:
        return
    text = (
        "Recurring skipped this month (bot was down on their day — log manually if needed):\n"
        + "\n".join(f"• {label} (day {day})" for label, day in payload)
    )
    await telegram_send(
        lambda: context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=text,
        ),
        context="missed recurring",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        logger.warning("Telegram network error while processing update: %s", context.error)
        return
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    settings: Settings = context.bot_data["settings"]
    await telegram_send(
        lambda: context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=f"Something went wrong: {context.error}",
        ),
        context="error notification",
    )


def build_application(settings: Settings, recurring_yaml_path: str = "recurring.yml") -> Application:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        load_recurring_rules(session, recurring_yaml_path)

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(register_bot_commands)
        .build()
    )
    application.bot_data["session_factory"] = session_factory
    application.bot_data["openai_client"] = OpenAI(api_key=settings.openai_api_key, timeout=30.0)
    application.bot_data["openai_model"] = settings.openai_model
    application.bot_data["timezone"] = settings.timezone
    application.bot_data["settings"] = settings
    application.bot_data["pending_confirmations"] = {}
    application.bot_data["pending_edits"] = {}

    register_handlers(application, settings.allowed_telegram_user_id)
    application.add_error_handler(error_handler)

    # misfire_grace_time=None: APScheduler's default grace window is 1 second.
    # run_once(..., when=0) targets "right now", so any startup delay past 1s
    # (normal — connecting to Telegram, etc.) causes the job to be silently
    # skipped entirely instead of run late, only logging a warning. All jobs
    # here are idempotent and safe to run whenever the process next gets to
    # them, so none should ever be silently dropped for running late.
    unlimited_grace = {"misfire_grace_time": None}
    application.job_queue.run_daily(
        recurring_job,
        time=_local_time(6, 0, settings.timezone),
        name="recurring_check",
        job_kwargs=unlimited_grace,
    )
    application.job_queue.run_once(
        recurring_job, when=0, name="recurring_check_startup", job_kwargs=unlimited_grace
    )
    application.job_queue.run_once(
        missed_recurring_job,
        when=0,
        name="missed_recurring_startup",
        job_kwargs=unlimited_grace,
    )
    application.job_queue.run_daily(
        backup_job,
        time=_local_time(3, 30, settings.timezone),
        name="daily_backup",
        job_kwargs=unlimited_grace,
    )
    application.job_queue.run_daily(
        backup_health_job,
        time=_local_time(12, 0, settings.timezone),
        name="backup_health",
        job_kwargs=unlimited_grace,
    )
    application.job_queue.run_once(
        backup_health_job, when=0, name="backup_health_startup", job_kwargs=unlimited_grace
    )

    return application


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Telegram's Bot API embeds the token in the request URL itself (no
    # Authorization header), and httpx logs full request URLs at INFO — every
    # getMe/getUpdates/sendMessage call would otherwise write the live bot
    # token straight into persisted container logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)
    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":
    main()
