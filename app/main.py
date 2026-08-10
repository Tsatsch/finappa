import logging
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from app.analytics import format_cents
from app.backup import create_snapshot, encrypt_file, prune_snapshots
from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db, make_engine
from app.handlers import register_handlers
from app.recurring import load_recurring_rules, run_recurring_check


def _local_time(hour: int, minute: int, tz_name: str) -> time:
    return time(hour=hour, minute=minute, tzinfo=ZoneInfo(tz_name))


async def recurring_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    session_factory = context.bot_data["session_factory"]
    settings: Settings = context.bot_data["settings"]
    with session_factory() as session:
        fired = run_recurring_check(session, settings.timezone)
        fired_values = [(rule.label, rule.amount_cents) for rule in fired]
    for label, amount_cents in fired_values:
        await context.bot.send_message(
            chat_id=settings.allowed_telegram_user_id,
            text=f"Recurring: logged {label} ({format_cents(amount_cents)}).",
        )


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    snapshot_dir = str(Path(settings.db_path).parent)
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

    prune_snapshots(snapshot_dir, keep=7)


def build_application(settings: Settings, recurring_yaml_path: str = "recurring.yml") -> Application:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        load_recurring_rules(session, recurring_yaml_path)

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["session_factory"] = session_factory
    application.bot_data["openai_client"] = OpenAI(api_key=settings.openai_api_key)
    application.bot_data["openai_model"] = settings.openai_model
    application.bot_data["timezone"] = settings.timezone
    application.bot_data["settings"] = settings

    register_handlers(application, settings.allowed_telegram_user_id)

    application.job_queue.run_daily(
        recurring_job, time=_local_time(6, 0, settings.timezone), name="recurring_check"
    )
    application.job_queue.run_once(recurring_job, when=0, name="recurring_check_startup")
    application.job_queue.run_daily(
        backup_job, time=_local_time(3, 30, settings.timezone), name="daily_backup"
    )

    return application


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":
    main()
