import asyncio
import csv
import io
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
from app.backup import backup_status_text
from app.telegram_io import telegram_send
from app.categories import CategoryName
from app.categorize import RecatError, find_matching_merchant, recat_merchant, resolve_category
from app.cleanup import (
    CleanupError,
    CleanupProposal,
    apply_merge,
    format_cleanup_prompt,
    propose_merges,
    reject_proposal,
    start_cleanup_session,
    undo_last_cleanup_session,
)
from app.db import Category, Merchant, Transaction, get_sessionmaker
from app.extractor import ExtractionError, extract_transactions
from app.ledger import (
    EDIT_USAGE,
    PendingCategoryEdit,
    apply_category_edit_merchant,
    apply_category_edit_row,
    delete_text,
    edit_text,
    find_text,
    logged_item_line,
    recent_text,
    resolve_transaction_ts,
)
from app.config import Settings

HELP_TEXT = (
    "Send a message describing one or more transactions, e.g. "
    '"30€ Rewe and 15€ fuel" or "yesterday 30€ Rewe".\n\n'
    "Commands:\n"
    "/summary [YYYY-MM] — income, expenses, and net (default: this month)\n"
    "/recent [n] — list recent transactions with IDs (default 10)\n"
    "/find <query> — search merchants and notes\n"
    "/delete <id> — remove one transaction\n"
    "/edit <id> amount|category|date <value> — fix a transaction\n"
    "/undo — remove the last message's transactions\n"
    "/export — CSV of all transactions\n"
    "/recat <merchant> <category> — fix a merchant's category, past and future\n"
    "/cleanup — review suggested merchant merges\n"
    "/cleanup_undo — undo the last cleanup session\n"
    "/backup_status — newest local backup and config flags\n"
)

# Telegram's "/" suggestion menu (set via setMyCommands on startup). Keep in
# sync with HELP_TEXT and register_handlers — omitting a command here hides it
# from the client autocomplete even though the handler still works.
BOT_COMMANDS = [
    BotCommand("start", "Show help and how to log transactions"),
    BotCommand("help", "Show help and how to log transactions"),
    BotCommand("summary", "Month summary; optional YYYY-MM"),
    BotCommand("recent", "List recent transactions with IDs"),
    BotCommand("find", "Search merchants and notes"),
    BotCommand("delete", "Remove one transaction by ID"),
    BotCommand("edit", "Fix amount, category, or date by ID"),
    BotCommand("undo", "Remove the last message's transactions"),
    BotCommand("export", "Download CSV of all transactions"),
    BotCommand("recat", "Fix a merchant's category, past and future"),
    BotCommand("cleanup", "Review suggested merchant merges"),
    BotCommand("cleanup_undo", "Undo the last cleanup session"),
    BotCommand("backup_status", "Newest local backup and config flags"),
]

SUMMARY_USAGE = "Usage: /summary [YYYY-MM]"


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
    session_factory,
    openai_client: OpenAI,
    openai_model: str,
    reasoning_effort: str,
    tz_name: str,
    text: str,
    update_id: int,
) -> ProcessResult:
    try:
        items = extract_transactions(
            openai_client,
            openai_model,
            text,
            tz_name,
            reasoning_effort=reasoning_effort,
        )
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


def summary_text(session_factory, tz_name: str, month: str | None = None) -> str:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    if month is None:
        with session_factory() as session:
            return format_summary_message(monthly_summary(session, tz_name))
    try:
        year_s, month_s = month.split("-", 1)
        if len(year_s) != 4 or len(month_s) != 2:
            return SUMMARY_USAGE
        year, mon = int(year_s), int(month_s)
        if not 1 <= mon <= 12:
            return SUMMARY_USAGE
        target = date(year, mon, 1)
    except (ValueError, TypeError):
        return SUMMARY_USAGE
    current = date(now_local.year, now_local.month, 1)
    if target > current:
        return "That month is in the future."
    at = datetime(year, mon, 1, 12, 0, tzinfo=tz)
    with session_factory() as session:
        body = format_summary_message(monthly_summary(session, tz_name, at=at))
    if target == current:
        return body
    return f"Summary for {year:04d}-{mon:02d}:\n{body}"


def backup_status_command_text(settings: Settings) -> str:
    snapshot_dir = str(Path(settings.db_path).parent)
    return backup_status_text(
        snapshot_dir,
        channel_configured=settings.backup_channel_id is not None,
        key_configured=bool(settings.backup_encryption_key),
        tz_name=settings.timezone,
    )


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
        context.bot_data["openai_reasoning_effort"],
        context.bot_data["timezone"],
        update.message.text,
        update.update_id,
    )
    if result.reply_text:
        await telegram_send(
            lambda: update.message.reply_text(result.reply_text),
            ledger_written=True,
            context=f"log confirm update_id={update.update_id}",
        )
    for pending in result.pending:
        context.bot_data["pending_confirmations"][pending.pending_id] = pending
        await telegram_send(
            lambda p=pending: update.message.reply_text(
                _pending_prompt_text(p), reply_markup=_category_keyboard(p.pending_id)
            ),
            context=f"category prompt update_id={update.update_id}",
        )


async def handle_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and len(context.args) > 1:
        reply = SUMMARY_USAGE
    else:
        month = context.args[0] if context.args else None
        reply = summary_text(
            context.bot_data["session_factory"],
            context.bot_data["timezone"],
            month=month,
        )
    await telegram_send(lambda: update.message.reply_text(reply), context="/summary")


async def handle_backup_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = backup_status_command_text(context.bot_data["settings"])
    await telegram_send(lambda: update.message.reply_text(reply), context="/backup_status")


async def handle_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else ""
    reply = find_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        query,
    )
    await telegram_send(lambda: update.message.reply_text(reply), context="/find")


async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = undo_last(context.bot_data["session_factory"])
    await telegram_send(
        lambda: update.message.reply_text(reply),
        ledger_written=reply.startswith("Removed"),
        context=f"/undo update_id={update.update_id}",
    )


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    csv_buffer = export_csv(context.bot_data["session_factory"])
    await telegram_send(
        lambda: update.message.reply_document(document=csv_buffer, filename="finappa_export.csv"),
        context="/export",
    )


async def handle_recat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await telegram_send(lambda: update.message.reply_text("Usage: /recat <merchant> <category>"))
        return
    *merchant_parts, category_name = args
    reply = recat_text(context.bot_data["session_factory"], " ".join(merchant_parts), category_name)
    await telegram_send(
        lambda: update.message.reply_text(reply),
        ledger_written=reply.endswith("updated."),
        context=f"/recat update_id={update.update_id}",
    )


def _start_cleanup(
    session_factory,
    openai_client: OpenAI,
    model: str,
    reasoning_effort: str,
) -> tuple[int | None, list[CleanupProposal]]:
    with session_factory() as session:
        proposals = propose_merges(
            openai_client, model, reasoning_effort, session
        )
        if not proposals:
            return None, []
        cleanup_session = start_cleanup_session(session)
        return cleanup_session.id, proposals


def _cleanup_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Apply", callback_data=f"pclean:{token}:APPLY"
                ),
                InlineKeyboardButton(
                    "Skip", callback_data=f"pclean:{token}:SKIP"
                ),
            ],
            [InlineKeyboardButton("Done", callback_data=f"pclean:{token}:DONE")],
        ]
    )


async def handle_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id, proposals = await asyncio.to_thread(
        _start_cleanup,
        context.bot_data["session_factory"],
        context.bot_data["openai_client"],
        context.bot_data["openai_model_heavy"],
        context.bot_data["openai_reasoning_effort_heavy"],
    )
    if not proposals:
        await telegram_send(
            lambda: update.message.reply_text("Nothing to clean up."),
            context="/cleanup",
        )
        return

    token = secrets.token_hex(4)
    context.bot_data["pending_cleanups"][token] = {
        "session_id": session_id,
        "proposals": proposals,
        "index": 0,
        "applied": 0,
        "skipped": 0,
    }
    await telegram_send(
        lambda: update.message.reply_text(
            format_cleanup_prompt(proposals[0], 0, len(proposals)),
            reply_markup=_cleanup_keyboard(token),
        ),
        context="/cleanup",
    )


def _apply_cleanup_choice(
    session_factory,
    session_id: int,
    proposal: CleanupProposal,
    choice: str,
) -> str | None:
    with session_factory() as session:
        if choice == "APPLY":
            return apply_merge(
                session, session_id, proposal.from_name, proposal.to_name
            )
        reject_proposal(session, proposal.from_name, proposal.to_name)
        return None


def _undo_cleanup(session_factory) -> str:
    with session_factory() as session:
        return undo_last_cleanup_session(session)


async def handle_cleanup_undo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        reply = await asyncio.to_thread(
            _undo_cleanup, context.bot_data["session_factory"]
        )
    except CleanupError as exc:
        reply = str(exc)
    await telegram_send(
        lambda: update.message.reply_text(reply),
        ledger_written=reply.startswith("Restored"),
        context="/cleanup_undo",
    )


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
    await telegram_send(lambda: update.message.reply_text(reply), context="/recent")


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await telegram_send(lambda: update.message.reply_text("Usage: /delete <id>"))
        return
    reply = delete_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        int(context.args[0]),
    )
    await telegram_send(
        lambda: update.message.reply_text(reply),
        ledger_written=reply.startswith("Deleted"),
        context=f"/delete update_id={update.update_id}",
    )


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 3 or not args[0].isdigit():
        await telegram_send(lambda: update.message.reply_text(EDIT_USAGE))
        return
    outcome = edit_text(
        context.bot_data["session_factory"],
        context.bot_data["timezone"],
        int(args[0]),
        args[1],
        " ".join(args[2:]),
    )
    if outcome.pending is not None:
        pending_id = secrets.token_hex(4)
        pending = PendingCategoryEdit(
            pending_id=pending_id,
            tx_id=outcome.pending.tx_id,
            category_name=outcome.pending.category_name,
            merchant_id=outcome.pending.merchant_id,
            merchant_display=outcome.pending.merchant_display,
        )
        context.bot_data["pending_edits"][pending_id] = pending
        await telegram_send(
            lambda: update.message.reply_text(
                outcome.reply, reply_markup=_edit_category_keyboard(pending_id)
            ),
            context=f"/edit confirm update_id={update.update_id}",
        )
        return
    await telegram_send(
        lambda: update.message.reply_text(outcome.reply),
        ledger_written=outcome.reply.startswith("Updated"),
        context=f"/edit update_id={update.update_id}",
    )


def _edit_category_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "This row only", callback_data=f"pedit:{pending_id}:ROW"
                )
            ],
            [
                InlineKeyboardButton(
                    "Merchant + all past", callback_data=f"pedit:{pending_id}:MERCHANT"
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"pedit:{pending_id}:CANCEL")],
        ]
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await telegram_send(lambda: update.message.reply_text(HELP_TEXT))


async def handle_category_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["settings"].allowed_telegram_user_id:
        return
    await telegram_send(lambda: query.answer())
    _, pending_id, choice = query.data.split(":", 2)
    pending = context.bot_data["pending_confirmations"].pop(pending_id, None)
    if pending is None:
        await telegram_send(
            lambda: query.edit_message_text("This confirmation expired — just resend the message.")
        )
        return
    if choice == "SKIP":
        label = _pending_label(pending)
        await telegram_send(lambda: query.edit_message_text(f"Discarded: {label}, not logged."))
        return
    reply = await asyncio.to_thread(
        confirm_category,
        context.bot_data["session_factory"],
        pending,
        choice,
        context.bot_data["timezone"],
        update.update_id,
    )
    await telegram_send(
        lambda: query.edit_message_text(reply),
        ledger_written=True,
        context=f"category confirm update_id={update.update_id}",
    )


async def handle_edit_category_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["settings"].allowed_telegram_user_id:
        return
    await telegram_send(lambda: query.answer())
    _, pending_id, choice = query.data.split(":", 2)
    pending = context.bot_data["pending_edits"].pop(pending_id, None)
    if pending is None:
        await telegram_send(
            lambda: query.edit_message_text(
                "This edit confirmation expired — run /edit again."
            )
        )
        return
    if choice == "CANCEL":
        await telegram_send(lambda: query.edit_message_text("Edit cancelled."))
        return
    if choice == "ROW":
        reply = await asyncio.to_thread(
            apply_category_edit_row,
            context.bot_data["session_factory"],
            context.bot_data["timezone"],
            pending.tx_id,
            pending.category_name,
        )
    elif choice == "MERCHANT":
        reply = await asyncio.to_thread(
            apply_category_edit_merchant,
            context.bot_data["session_factory"],
            context.bot_data["timezone"],
            pending.tx_id,
            pending.category_name,
            pending.merchant_id,
        )
    else:
        await telegram_send(lambda: query.edit_message_text("Unknown choice."))
        return
    await telegram_send(
        lambda: query.edit_message_text(reply),
        ledger_written=reply.startswith("Updated"),
        context=f"/edit confirm choice={choice}",
    )


async def handle_cleanup_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["settings"].allowed_telegram_user_id:
        return
    await telegram_send(lambda: query.answer())
    _, token, choice = query.data.split(":", 2)
    pending = context.bot_data["pending_cleanups"].get(token)
    if pending is None:
        await telegram_send(
            lambda: query.edit_message_text(
                "This cleanup expired — run /cleanup again."
            )
        )
        return

    if choice == "DONE":
        context.bot_data["pending_cleanups"].pop(token, None)
        await telegram_send(
            lambda: query.edit_message_text(
                f"Cleanup done: {pending['applied']} applied, "
                f"{pending['skipped']} skipped."
            )
        )
        return
    if choice not in {"APPLY", "SKIP"}:
        await telegram_send(lambda: query.edit_message_text("Unknown choice."))
        return

    proposal = pending["proposals"][pending["index"]]
    try:
        merge_line = await asyncio.to_thread(
            _apply_cleanup_choice,
            context.bot_data["session_factory"],
            pending["session_id"],
            proposal,
            choice,
        )
    except CleanupError as exc:
        await telegram_send(lambda: query.edit_message_text(str(exc)))
        return

    counter = "applied" if choice == "APPLY" else "skipped"
    pending[counter] += 1
    pending["index"] += 1
    if pending["index"] >= len(pending["proposals"]):
        context.bot_data["pending_cleanups"].pop(token, None)
        summary = (
            f"Cleanup done: {pending['applied']} applied, "
            f"{pending['skipped']} skipped."
        )
        text = f"{merge_line}\n{summary}" if merge_line else summary
        await telegram_send(
            lambda: query.edit_message_text(text),
            ledger_written=choice == "APPLY",
            context=f"/cleanup choice={choice}",
        )
        return

    next_prompt = format_cleanup_prompt(
        pending["proposals"][pending["index"]],
        pending["index"],
        len(pending["proposals"]),
    )
    text = f"{merge_line}\n\n{next_prompt}" if merge_line else next_prompt
    await telegram_send(
        lambda: query.edit_message_text(
            text, reply_markup=_cleanup_keyboard(token)
        ),
        ledger_written=choice == "APPLY",
        context=f"/cleanup choice={choice}",
    )


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
    application.add_handler(CommandHandler("find", handle_find, filters=user_filter))
    application.add_handler(CommandHandler("backup_status", handle_backup_status, filters=user_filter))
    application.add_handler(CommandHandler("undo", handle_undo, filters=user_filter))
    application.add_handler(CommandHandler("export", handle_export, filters=user_filter))
    application.add_handler(CommandHandler("recat", handle_recat, filters=user_filter))
    application.add_handler(CommandHandler("cleanup", handle_cleanup, filters=user_filter))
    application.add_handler(
        CommandHandler("cleanup_undo", handle_cleanup_undo, filters=user_filter)
    )
    application.add_handler(CommandHandler("recent", handle_recent, filters=user_filter))
    application.add_handler(CommandHandler("delete", handle_delete, filters=user_filter))
    application.add_handler(CommandHandler("edit", handle_edit, filters=user_filter))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_category_confirmation, pattern=r"^pconf:"))
    application.add_handler(
        CallbackQueryHandler(handle_edit_category_confirmation, pattern=r"^pedit:")
    )
    application.add_handler(
        CallbackQueryHandler(handle_cleanup_confirmation, pattern=r"^pclean:")
    )