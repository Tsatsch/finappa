import asyncio
import csv
import io
from datetime import datetime, timezone

from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.analytics import format_cents, format_summary_message, monthly_summary
from app.categorize import RecatError, recat_merchant, resolve_category
from app.db import Category, Merchant, Transaction, get_sessionmaker
from app.extractor import ExtractionError, extract_transactions

HELP_TEXT = (
    "Send a message describing one or more transactions, e.g. "
    '"30€ Rewe and 15€ fuel".\n\n'
    "Commands:\n"
    "/summary — this month's income, expenses, and net\n"
    "/undo — remove the last message's transactions\n"
    "/export — CSV of all transactions\n"
    "/recat <merchant> <category> — fix a merchant's category, past and future\n"
)


def process_message(
    session_factory, openai_client: OpenAI, openai_model: str, tz_name: str, text: str, update_id: int
) -> str:
    try:
        items = extract_transactions(openai_client, openai_model, text)
    except ExtractionError as exc:
        return exc.user_message

    now = datetime.now(timezone.utc)
    lines = []
    with session_factory() as session:
        for item in items:
            merchant, category = resolve_category(session, item.merchant, item.category)
            amount_cents = round(item.amount * 100)
            session.add(
                Transaction(
                    ts=now,
                    amount_cents=amount_cents,
                    type=item.type,
                    merchant_id=merchant.id if merchant else None,
                    category_id=category.id,
                    notes=item.notes,
                    source="message",
                    source_update_id=update_id,
                )
            )
            label = item.merchant or item.notes or item.type.title()
            lines.append(f"• {format_cents(amount_cents)} · {label} · {category.name}")
        session.commit()
        summary = monthly_summary(session, tz_name, at=now)

    header = f"Logged {len(items)}:"
    net_line = f"Net this month: {format_cents(summary['net_cents'])}"
    return "\n".join([header, *lines, net_line])


def summary_text(session_factory, tz_name: str) -> str:
    with session_factory() as session:
        summary = monthly_summary(session, tz_name)
    return format_summary_message(summary)


def undo_last(session_factory) -> str:
    with session_factory() as session:
        last = (
            session.query(Transaction)
            .filter(Transaction.source_update_id.isnot(None))
            .order_by(Transaction.id.desc())
            .first()
        )
        if last is None:
            return "Nothing to undo."
        update_id = last.source_update_id
        rows = session.query(Transaction).filter_by(source_update_id=update_id).all()
        count = len(rows)
        total_cents = sum(r.amount_cents for r in rows)
        for row in rows:
            session.delete(row)
        session.commit()
    return f"Removed {count} transaction(s) totaling {format_cents(total_cents)}."


def export_csv(session_factory) -> io.BytesIO:
    with session_factory() as session:
        rows = (
            session.query(
                Transaction.ts, Transaction.amount_cents, Transaction.type,
                Category.name, Merchant.display_name, Transaction.notes, Transaction.source,
            )
            .join(Category, Transaction.category_id == Category.id)
            .outerjoin(Merchant, Transaction.merchant_id == Merchant.id)
            .order_by(Transaction.ts)
            .all()
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "amount_eur", "type", "category", "merchant", "notes", "source"])
    for ts, amount_cents, type_, category_name, merchant_name, notes, source in rows:
        writer.writerow(
            [ts.isoformat(), f"{amount_cents / 100:.2f}", type_, category_name, merchant_name or "", notes or "", source]
        )

    return io.BytesIO(buffer.getvalue().encode("utf-8"))


def recat_text(session_factory, merchant_name: str, category_name: str) -> str:
    with session_factory() as session:
        try:
            merchant, count = recat_merchant(session, merchant_name, category_name)
        except RecatError as exc:
            return str(exc)
        return f"{merchant.display_name} → {category_name}, {count} past transaction(s) updated."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # process_message makes a blocking OpenAI HTTP call and blocking SQLite
    # work; run it off the event loop so a slow/hung OpenAI call doesn't stall
    # polling, other commands, and background jobs.
    reply = await asyncio.to_thread(
        process_message,
        context.bot_data["session_factory"],
        context.bot_data["openai_client"],
        context.bot_data["openai_model"],
        context.bot_data["timezone"],
        update.message.text,
        update.update_id,
    )
    await update.message.reply_text(reply)


async def handle_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = summary_text(context.bot_data["session_factory"], context.bot_data["timezone"])
    await update.message.reply_text(reply)


async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = undo_last(context.bot_data["session_factory"])
    await update.message.reply_text(reply)


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    csv_buffer = export_csv(context.bot_data["session_factory"])
    await update.message.reply_document(document=csv_buffer, filename="finappa_export.csv")


async def handle_recat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /recat <merchant> <category>")
        return
    *merchant_parts, category_name = args
    reply = recat_text(context.bot_data["session_factory"], " ".join(merchant_parts), category_name)
    await update.message.reply_text(reply)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


def register_handlers(application: Application, allowed_user_id: int) -> None:
    # filters.UpdateType.MESSAGE excludes edited messages. Without it, PTB
    # matches update.effective_message, which falls back to edited_message —
    # and update.message is None on an edit, so update.message.text /
    # .reply_text(...) would raise AttributeError. Edits also carry a new
    # update_id, so processing them would double-log rather than correct the
    # original entry; simplest correct behavior is to ignore edits entirely.
    user_filter = filters.User(user_id=allowed_user_id) & filters.UpdateType.MESSAGE
    application.add_handler(CommandHandler("start", handle_help, filters=user_filter))
    application.add_handler(CommandHandler("help", handle_help, filters=user_filter))
    application.add_handler(CommandHandler("summary", handle_summary, filters=user_filter))
    application.add_handler(CommandHandler("undo", handle_undo, filters=user_filter))
    application.add_handler(CommandHandler("export", handle_export, filters=user_filter))
    application.add_handler(CommandHandler("recat", handle_recat, filters=user_filter))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_message)
    )
