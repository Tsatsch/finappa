# Category Confirmation for Uncertain New Merchants — Design

Date: 2026-08-11
Status: Approved
Amends: [2026-08-10-telegram-finance-tracker-design.md](2026-08-10-telegram-finance-tracker-design.md)

## 1. Problem

The LLM extraction schema (`app/extractor.py::TransactionItem`) forces a category
choice via a closed `CategoryName` enum — there is no way for the model to say
"I'm not sure." For a merchant the bot has never seen, that guess becomes the
merchant's **canonical** category (spec §6.2) and silently colors every future
transaction there until manually corrected via `/recat`. The user wants the bot
to ask rather than guess when the category genuinely is a judgment call.

### Goals

- When categorizing a new (or merchant-less) transaction, if the model isn't
  confident, hold it and ask — never write an uncertain guess to the ledger.
- Asking is a tap, not a retype: inline category buttons, one of which is
  "don't log this."
- Zero interruption for the common case: a known merchant, or a confident new
  one, logs exactly as it does today.

### Non-goals

- Confidence on amount, merchant name, or transaction type. Confirmed
  (previous brainstorm round): these are rarely ambiguous once typed, and
  broadening scope here multiplies interruptions for little benefit.
- Re-asking about an already-known merchant. The stored category always wins
  over a fresh LLM guess (spec §6.2) — there is nothing to be uncertain about
  there, and `/recat` already exists for "I was wrong six months ago."
  Confirmed: yes-only-new/unknown.
- `/undo` for a pending confirmation. It was never written to the ledger — the
  "Don't log this" button *is* the undo-before-the-fact.
- A general button-driven shortcut UI. Buttons are used for exactly one closed-set
  choice (pick 1 of 14 categories); free text remains how every other
  interaction works. Explored and rejected during brainstorming — replacing
  free-text extraction with menus defeats the bot's entire premise (typing a
  sentence beats navigating a form).
- Surviving a container restart with pending confirmations intact. See §5.

## 2. Where uncertainty can matter

Confirmed during brainstorming: the LLM's category guess is only ever *used* in
two cases —

1. `item.merchant` doesn't fuzzy-match any known merchant (a new merchant is
   about to be created), or
2. `item.merchant` is empty (e.g. a salary deposit).

Every other case (fuzzy match hits) already ignores the LLM's category guess
entirely in favor of the stored one. So confirmation only ever triggers in
cases 1 and 2, combined with the model itself flagging low confidence.

## 3. Schema change (`app/extractor.py`)

```python
class TransactionItem(BaseModel):
    amount: float
    currency: str
    merchant: str
    category: CategoryName
    category_confident: bool
    type: Literal["EXPENSE", "INCOME", "REFUND"]
    notes: str | None = None
```

No default (Structured Outputs strict mode forbids one, same constraint that
already shaped `currency` — spec §5.1) — the model must set it explicitly every
time.

`SYSTEM_PROMPT` gains one instruction:

> Also set `category_confident` to true or false: false only when the category
> is a genuine judgment call between two or more categories, or the merchant
> gives no real signal for a category — not merely because the merchant is
> unfamiliar to you. Default to true whenever a category is a reasonably
> obvious fit.

The "not merely unfamiliar" framing matters: without it, every brand-new
merchant would trip low confidence, defeating the "zero interruption for the
confident case" goal.

Validation (`validate_extraction`, spec §5.3) is unchanged — confidence is not
a rejection reason, all-or-nothing validation still only fires on non-EUR or
out-of-bounds amounts.

## 4. Categorize changes (`app/categorize.py`)

`resolve_category` currently both looks up a fuzzy match *and* creates a new
merchant in one pass — there's no way to ask "would this match?" without also
committing to creating one if it doesn't. Split the lookup out:

```python
def find_matching_merchant(session: Session, merchant_name: str) -> Merchant | None:
    """Fuzzy-match merchant_name against known merchants. No side effects,
    no creation. Returns None for an empty name or no match."""
```

`resolve_category` is refactored to call this internally rather than
duplicating the match logic — same public signature and behavior as today,
now just built on the shared helper. This is the one deviation from the
original file's boundaries, and it's in-module: `categorize.py` already owns
merchant matching, this just factors out the read-only half of it.

## 5. Pipeline changes (`app/handlers.py`)

### 5.1 Held state, not a table

A pending confirmation is held in an **in-memory dict** on
`application.bot_data["pending_confirmations"]: dict[str, PendingConfirmation]`,
not a database table.

Rejected alternative: a `pending_confirmations` DB table, considered and
rejected. The failure mode of losing in-memory state on restart is "resend one
sentence" — nothing was ever written to the ledger, so there is no data to
lose. A table adds a migration-free-schema exception (spec's whole stance is
"one user, `create_all()` suffices," §14) to protect against a cost that
doesn't exist. If a stale button is tapped after a restart, the handler replies
"this expired, just resend it" instead of crashing.

```python
@dataclass
class PendingConfirmation:
    pending_id: str      # short random token, e.g. secrets.token_hex(4)
    ts: datetime          # original message arrival time, UTC — NOT confirmation time
    amount_cents: int
    type: str
    merchant: str          # raw display name as extracted; "" if none
    notes: str | None
```

`ts` is captured at extraction time and reused at confirmation time. The
existing non-goal "a transaction's timestamp is the time the message arrived"
(spec §1) still holds even if confirmation happens an hour later — this also
keeps month-boundary math correct for a message sent near midnight.

### 5.2 `process_message` return type changes

`process_message` currently returns `str`. It now returns:

```python
@dataclass
class ProcessResult:
    reply_text: str | None   # None if every item in the message was uncertain
    pending: list[PendingConfirmation]
```

This is a breaking change to an already-shipped, reviewed function and its
existing tests (`tests/test_handlers.py`) — the implementation plan must update
every caller and every existing assertion that currently reads `process_message`'s
return value as a bare string.

Per-item logic, replacing the current unconditional insert loop:

- Known merchant (fuzzy match hits) → unchanged: insert immediately using the
  stored category, ignore `category_confident` entirely.
- New/no merchant, `category_confident: true` → unchanged: insert immediately
  using the LLM's category (today's behavior, merchant becomes canonical).
- New/no merchant, `category_confident: false` → **do not insert.** Build a
  `PendingConfirmation` (new `pending_id`, original `ts`, the item's
  `amount_cents`/`type`/`merchant`/`notes`) and append it to `pending`. Do not
  call `resolve_category` for this item — no `Merchant` row is created yet.

If one or more items were logged, `reply_text` is the existing
`"Logged N: ...\nNet this month: ..."` text, computed from whatever *is* in the
database — pending items are invisible to `monthly_summary` until confirmed, so
no special-casing is needed there. If zero items were logged (every item in the
message was uncertain), `reply_text` is `None` — no "Logged 0" message.

### 5.3 Sending the confirmation prompts

`handle_message` (the async wrapper) is responsible for turning `ProcessResult`
into Telegram messages — `process_message` itself stays PTB-free and testable
exactly like today, per the plain-function/thin-wrapper split established in
Task 8.

```python
async def handle_message(update, context):
    result = await asyncio.to_thread(process_message, ...)
    if result.reply_text:
        await update.message.reply_text(result.reply_text)
    for pending in result.pending:
        context.bot_data["pending_confirmations"][pending.pending_id] = pending
        await update.message.reply_text(
            _pending_prompt_text(pending),
            reply_markup=_category_keyboard(pending.pending_id),
        )
```

`_pending_prompt_text` renders something like:

```
Not sure about the category for 30,00 € · Rewe City — which fits?
```

`_category_keyboard(pending_id)` builds an `InlineKeyboardMarkup`: the 14
`CategoryName` values (2 per row) plus a final "Don't log this" row.
`callback_data` format: `pconf:{pending_id}:{category_value_or_SKIP}` — well
under Telegram's 64-byte limit (longest category name is 13 characters).

### 5.4 Resolving a tap

One new handler, `handle_category_confirmation`, registered via
`CallbackQueryHandler(handle_category_confirmation, pattern=r"^pconf:")`.

`CallbackQueryHandler` does not compose with `filters.User` the way
`CommandHandler`/`MessageHandler` do (spec §2.2's mechanism is
message/command-specific) — this handler checks
`update.callback_query.from_user.id != allowed_user_id` explicitly as its
first line and returns early otherwise. This is spelled out here rather than
left to "verify at implementation time" because getting auth wrong on the one
new interaction surface this feature adds would be a real security gap, not a
minor implementation detail.

```python
async def handle_category_confirmation(update, context):
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
        await query.edit_message_text(f"Discarded: {pending.merchant or pending.notes or 'that item'}, not logged.")
        return
    reply = confirm_category(
        context.bot_data["session_factory"], pending, choice, context.bot_data["timezone"]
    )
    await query.edit_message_text(reply)
```

`confirm_category` (new plain function in `handlers.py`, same testability
pattern as `recat_text`/`undo_last`):

```python
def confirm_category(session_factory, pending: PendingConfirmation, category_name: str, tz_name: str) -> str:
```

Inserts the transaction using `pending.ts`, `pending.amount_cents`,
`pending.type`, `pending.notes`, and `resolve_category(session, pending.merchant, CategoryName(category_name))`
— reusing `resolve_category` here means the confirmed category becomes the
merchant's canonical one going forward, exactly like an auto-logged confident
item would (spec §6.2 unchanged). Returns confirmation text with the
month-to-date net, same shape as `process_message`'s existing reply.

`query.edit_message_text` replaces the button message in place — the buttons
disappear once answered, replaced by the result. Standard PTB pattern, no new
dependency.

## 6. Multi-item behavior (confirmed)

A message with one confident item and one uncertain item: the confident item
logs immediately as part of the normal `"Logged 1: ..."` reply; the uncertain
item gets its own separate confirmation message with its own buttons.
Confirmed over "hold the whole message" — a confirmation is resolved by a tap,
not a resend, so there's no double-log risk the way there is with the existing
validation-rejection all-or-nothing rule (spec §5.3, a genuinely different
case: rejection means *nothing was extracted correctly*, uncertainty means
*extraction succeeded but one judgment call is open*).

## 7. Documentation

`AGENTS.md` gains one invariant:

> Uncertain new-merchant categorization holds for confirmation, never guesses.
> A new or merchant-less item where the LLM sets `category_confident: false`
> is never inserted until the user taps a category button (or discards it).
> Known merchants and confident guesses log immediately, unchanged.

And one "do not add" entry: no DB-backed pending-confirmation table — see §5.1
for why the in-memory version is the deliberate choice, not an oversight.

Since `CLAUDE.md` and `.cursor/rules/finappa.mdc` both point at `AGENTS.md`
rather than duplicating it (spec §12), this one edit keeps Claude Code and
Cursor consistent with no separate Cursor-specific work.

## 8. Testing

| Test | Asserts |
|---|---|
| `test_extractor` | `category_confident` round-trips through the schema (both true/false); validation behavior is unchanged by its presence. |
| `test_categorize` | `find_matching_merchant` returns `None` on no match/empty name, the matched `Merchant` otherwise; `resolve_category`'s existing tests still pass against the refactored implementation. |
| `test_handlers` | Confident new-merchant item still auto-logs (regression against Task 8's existing test); an unconfident new-merchant item is **not** inserted and appears in `ProcessResult.pending`; a mixed message logs the confident item in `reply_text` and returns the unconfident one in `pending` only; `confirm_category` inserts using the *pending* `ts` (not "now") and the tapped category, and makes that category canonical for the merchant going forward. |

The `SKIP` path and the "expired" path are PTB-layer branches with no
underlying plain function — consistent with the existing accepted pattern
where thin async-wrapper branches (e.g. `handle_recat`'s arg-count check) don't
get direct test coverage.

## 9. Rejected, with reasons

| Rejected | Reason |
|---|---|
| Confidence on every field (amount/merchant/type) | Confirmed non-goal — these are rarely ambiguous once typed; scope stays on the one genuinely fuzzy judgment call (§1). |
| Re-confirming known merchants | The stored category already overrides any fresh guess; `/recat` exists for correcting history (§1). |
| Text-reply confirmation ("type the category name") | Closed-set choice; buttons can't be typo'd and need no fuzzy-matching-of-a-fuzzy-matcher (§5.3 discussion during brainstorming). |
| Log provisionally, correct after | Reintroduces the "maybe-wrong data sitting in the ledger" risk this whole feature exists to remove. |
| DB-backed pending-confirmation table | Failure mode of the in-memory version is "resend one sentence," not data loss — a table buys durability the low stakes don't need (§5.1). |
| General button-driven shortcut UI | Different feature; replacing free text with menus defeats the bot's premise (§1 non-goals). |
| Hold the whole message until every item resolves | A confirmation is a tap, not a resend — no double-log risk that would justify blocking the confident item too (§6). |
