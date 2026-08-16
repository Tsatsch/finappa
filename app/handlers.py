import asyncio
import csv
import io
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from openai import OpenAI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.analytics import format_cents, format_summary_message, monthly_summary
from app.categories import CategoryName
from app.categorize import RecatError, find_matching_merchant, recat_merchant, resolve_category
from app.db import Category, Merchant, Transaction, get_sessionmaker
from app.extractor import ExtractionError, extract_transactions
from app.ledger import (
    EDIT_USAGE,
    delete_text,
    edit_text,
    logged_item_line,
    recent_text,
    resolve_transaction_ts,
)

HELP_TEXT = (
    "Send a message describing one or more transactions, e.g. "
    '"30€ Rewe and 15€ fuel" or "yesterday 30€ Rewe".\n\n'
    "Commands:\n"
    "/summary — this month's income, expenses, and net\n"
    "/recent [n] — list recent transactions with IDs (default 10)\n"
    "/delete <id> — remove one transaction\n"
    "/edit <id> amount|category|date <value> — fix a transaction\n"
    "/undo — remove the last message's transactions\n"
    "/export — CSV of all transactions\n"
    "/recat <merchant> <category> — fix a merchant's category, past and future\n"
)

# Telegram's "/" suggestion menu (set via setMyCommands on startup). Keep in
# sync with HELP_TEXT and register_handlers — omitting a command here hides it
# from the client autocomplete even though the handler still works.
BOT_COMMANDS = [
    BotCommand("start", "Show help and how to log transactions"),
    BotCommand("help", "Show help and how to log transactions"),
    BotCommand("summary", "This month's income, expenses, and net"),
    BotCommand("recent", "List recent transactions with IDs"),
    BotCommand("delete", "Remove one transaction by ID"),
    BotCommand("edit", "Fix amount, category, or date by ID"),
    BotCommand("undo", "Remove the last message's transactions"),
    BotCommand("export", "Download CSV of all transactions"),
    BotCommand("recat", "Fix a merchant's category, past and future"),
]


@dataclass
class PendingConfirmation:
    pending_id: str
    ts: datetime
    amount_cents: int
    type: str
    merchant: str
    notes: str | None


@dataclass
class ProcessResult:
    reply_text: str | None
    pending: list[PendingConfirmation]


def _pending_label(pending: PendingConfirmation) -> str:
    return pending.merchant or pending.notes or pending.type.title()


def process_message(
    session_factory, openai_client: OpenAI, openai_model: str, tz_name: str, text: str, update_id: int
) -> ProcessResult:
    try:
        items = extract_transactions(openai_client, openai_model, text, tz_name)
    except ExtractionError as exc:
        return ProcessResult(reply_text=exc.user_message, pending=[])

    now = datetime.now(timezone.utc)
    lines = []
    pending: list[PendingConfirmation] = []
    with session_factory() as session:
        for item in items:
            amount_cents = round(item.amount * 100)
            ts = resolve_transaction_ts(item.local_date, tz_name, now)
            existing_merchant = find_matching_merchant(session, item.merchant)

            if existing_merchant is None and not item.category_confident:
                pending.append(
                    PendingConfirmation(
                        pending_id=secrets.token_hex(4),
                        ts=ts,
                        amount_cents=amount_cents,
                        type=item.type,
                        merchant=item.merchant,
                        notes=item.notes,
                    )
                )
                continue

            merchant, category = resolve_category(session, item.merchant, item.category)
            session.add(
                Transaction(
                    ts=ts,
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
            lines.append(logged_item_line(amount_cents, label, category.name, ts, tz_name))
        session.commit()

        reply_text = None
        if lines:
            summary = monthly_summary(session, tz_name, at=now)
            header = f"Logged {len(lines)}:"
            net_line = f"Net this month: {format_cents(summary['net_cents'])}"
            reply_text = "\n".join([header, *lines, net_line])

    return ProcessResult(reply_text=reply_text, pending=pending)


def confirm_category(
    session_factory, pending: PendingConfirmation, category_name: str, tz_name: str, update_id: int
) -> str:
    category_enum = CategoryName(category_name)
    with session_factory() as session:
        merchant, category = resolve_category(session, pending.merchant, category_enum)
        # resolve_category returns the merchant's already-stored category when
        # one exists — it can differ from category_name (what the user just
        # tapped). Read .name now, while the session is still open, so the
        # reply always reflects what was actually written to Transaction.
        stored_category_name = category.name
        session.add(
            Transaction(
                ts=pending.ts,
                amount_cents=pending.amount_cents,
                type=pending.type,
                merchant_id=merchant.id if merchant else None,
                category_id=category.id,
                notes=pending.notes,
                source="message",
                source_update_id=update_id,
            )
        )
        session.commit()
        summary = monthly_summary(session, tz_name, at=pending.ts)

    label = _pending_label(pending)
    return (
        f"Logged: {format_cents(pending.amount_cents)} · {label} · {stored_category_name}\n"
        f"Net this month: {format_cents(summary['net_cents'])}"
    )


def _pending_prompt_text(pending: PendingConfirmation) -> str:
    label = _pending_label(pending)
    return f"Not sure about the category for {format_cents(pending.amount_cents)} · {label} — which fits?"


def _category_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(cat.value, callback_data=f"pconf:{pending_id}:{cat.value}")
        for cat in CategoryName
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Don't log this", callback_data=f"pconf:{pending_id}:SKIP")])
    return InlineKeyboardMarkup(rows)


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
    result = await asyncio.to_thread(
        process_message,
        context.bot_data["session_factory"],
        context.bot_data["openai_client"],
        context.bot_data["openai_model"],
        context.bot_data["timezone"],
        update.message.text,
        update.update_id,
    )
    if result.reply_text:
        await update.message.reply_text(result.reply_text)
    for pending in result.pending:
        context.bot_data["pending_confirmations"][pending.pending_id] = pending
        await update.message.reply_text(
            _pending_prompt_text(pending), reply_markup=_category_keyboard(pending.pending_id)
        )


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


async def handle_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            pass
    reply = recent_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        limit,
    )
    await update.message.reply_text(reply)


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete <id>")
        return
    reply = delete_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        int(context.args[0]),
    )
    await update.message.reply_text(reply)


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 3 or not args[0].isdigit():
        await update.message.reply_text(EDIT_USAGE)
        return
    reply = edit_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        int(args[0]),
        args[1],
        " ".join(args[2:]),
    )
    await update.message.reply_text(reply)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def handle_category_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["settings"].allowed_telegram_user_id:
        return
    await query.answer()
    _, pending_id, choice = query.data.split(":", 2)
    pending = context.bot_data["pending_confirmations"].pop(pending_id, None)
    if pending is None:
        await query.edit_message_text("This confirmation expired — just resend the message.")
        return
    if choice == "SKIP":
        label = _pending_label(pending)
        await query.edit_message_text(f"Discarded: {label}, not logged.")
        return
    reply = await asyncio.to_thread(
        confirm_category,
        context.bot_data["session_factory"],
        pending,
        choice,
        context.bot_data["timezone"],
        update.update_id,
    )
    await query.edit_message_text(reply)


async def register_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


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
    application.add_handler(CommandHandler("recent", handle_recent, filters=user_filter))
    application.add_handler(CommandHandler("delete", handle_delete, filters=user_filter))
    application.add_handler(CommandHandler("edit", handle_edit, filters=user_filter))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_category_confirmation, pattern=r"^pconf:"))
