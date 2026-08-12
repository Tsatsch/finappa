---
name: add-command
description: Add a new Telegram bot command to finappa. Use when the user wants a new /command added to the bot.
---

# Add a command

`app/handlers.py` keeps every command's real logic in a plain function
(`session_factory` in, string or file out) with a thin `async def handle_*`
wrapper that PTB actually calls. Follow that split — it's what keeps commands
testable without constructing `telegram.Update` objects. See existing commands
(`summary_text`, `undo_last`, `recat_text`) as the pattern to copy.

1. Write the plain function in `app/handlers.py`:
   ```python
   def my_command_text(session_factory, ...) -> str:
       with session_factory() as session:
           ...  # query/mutate
       return "reply text"
   ```
2. Write the thin async wrapper below it:
   ```python
   async def handle_my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
       reply = my_command_text(context.bot_data["session_factory"], ...)
       await update.message.reply_text(reply)
   ```
   Anything the wrapper needs beyond `session_factory` should already be in
   `context.bot_data` (see the keys `build_application` sets in `app/main.py`);
   add a new key there if the command needs something new.
3. Register it in `register_handlers`:
   ```python
   application.add_handler(CommandHandler("mycommand", handle_my_command, filters=user_filter))
   ```
4. Add it to `HELP_TEXT` and `BOT_COMMANDS` in `app/handlers.py` so `/help`
   and Telegram's slash-menu suggestions stay accurate.
5. Write a test for the plain function in `tests/test_handlers.py`, following the
   existing tests' pattern (`session_factory` fixture backed by a `tmp_path`
   SQLite file — never `:memory:`, WAL mode needs a real file).
6. Run `pytest -v`, then deploy (see the `deploy` skill).
