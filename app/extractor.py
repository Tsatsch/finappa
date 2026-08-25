from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from openai import OpenAI
from pydantic import BaseModel

from app.categories import CategoryName

MAX_AMOUNT = 1_000_000


class TransactionItem(BaseModel):
    amount: float
    currency: str
    merchant: str
    category: CategoryName
    category_confident: bool
    type: Literal["EXPENSE", "INCOME"]
    notes: str | None = None
    local_date: date | None = None


class ExtractionResult(BaseModel):
    transactions: list[TransactionItem]


class ExtractionError(Exception):
    """Raised for both OpenAI API failures and boundary-validation failures
    (spec §5.3). `.user_message` is ready to send to the user as-is."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


SYSTEM_PROMPT = (
    "You extract financial transactions from a message written by the user to "
    "their personal finance tracker. The message may describe one or several "
    "transactions (expenses or income). For each transaction, extract "
    "the amount, currency (ISO 4217 code, e.g. EUR, USD), the merchant or "
    "payer/payee name (empty string if there is none, e.g. a salary deposit), "
    "the category (pick the single best fit from the given list), the type "
    "(EXPENSE or INCOME), any short note worth keeping, and local_date (YYYY-MM-DD) "
    "when the user states when something happened (e.g. yesterday, last Monday, "
    "Aug 5) — use the calendar day in the user's local sense; leave local_date null "
    "when no date is mentioned. If the "
    "message describes no transactions, return an empty list. Also set "
    "category_confident to true or false: false only when the category is a "
    "genuine judgment call between two or more categories, or the merchant "
    "gives no real signal for a category — not merely because the merchant is "
    "unfamiliar to you. Default to true whenever a category is a reasonably "
    "obvious fit."
)


def _client_parse(
    client: OpenAI,
    model: str,
    text: str,
    reasoning_effort: str | None = None,
) -> ExtractionResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    parse = getattr(client.chat.completions, "parse", None)
    if parse is None:  # pragma: no cover - depends on installed openai-python version
        parse = client.beta.chat.completions.parse
    kwargs = dict(model=model, messages=messages, response_format=ExtractionResult)
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    try:
        completion = parse(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        completion = parse(**kwargs)
    return completion.choices[0].message.parsed


def _fmt_echo_amount(amount: float, currency: str) -> str:
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} €" if currency == "EUR" else f"{formatted} {currency}"


def _echo_items(items: list[TransactionItem]) -> str:
    parts = []
    for item in items:
        label = item.merchant or item.notes or item.type.title()
        parts.append(f"{_fmt_echo_amount(item.amount, item.currency)} {label}")
    return " · ".join(parts)


def validate_extraction(result: ExtractionResult, tz_name: str = "Europe/Berlin") -> list[TransactionItem]:
    if not result.transactions:
        raise ExtractionError("I didn't find any transactions in that message.")
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    for item in result.transactions:
        if item.currency != "EUR":
            raise ExtractionError(
                f"Nothing logged — '{item.merchant or item.notes or 'one item'}' came "
                f"back as {item.currency}, and I only handle EUR.\n"
                f"I read: {_echo_items(result.transactions)}"
            )
        if item.amount <= 0 or item.amount > MAX_AMOUNT:
            raise ExtractionError(
                f"Nothing logged — an amount of {item.amount} doesn't look right.\n"
                f"I read: {_echo_items(result.transactions)}"
            )
        if item.local_date is not None and item.local_date > today:
            raise ExtractionError(
                "Nothing logged — that date is in the future.\n"
                f"I read: {_echo_items(result.transactions)}"
            )
    return result.transactions


def extract_transactions(
    client: OpenAI,
    model: str,
    text: str,
    tz_name: str = "Europe/Berlin",
    reasoning_effort: str | None = None,
) -> list[TransactionItem]:
    try:
        result = _client_parse(
            client,
            model,
            text,
            reasoning_effort=reasoning_effort,
        )
        return validate_extraction(result, tz_name)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"Couldn't reach the AI extractor ({exc.__class__.__name__}). "
            "Nothing logged — try again."
        ) from exc
