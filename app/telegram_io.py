import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telegram.error import NetworkError

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def telegram_send(
    send: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    ledger_written: bool = False,
    context: str = "",
) -> T | None:
    """Send to Telegram with retries. Returns None if all attempts hit NetworkError."""
    last: NetworkError | None = None
    for attempt in range(retries):
        try:
            return await send()
        except NetworkError as exc:
            last = exc
            if attempt + 1 < retries:
                await asyncio.sleep(0.5 * (2**attempt))
    suffix = f" ({context})" if context else ""
    if ledger_written:
        logger.error("Ledger changed but Telegram unreachable%s: %s", suffix, last)
    else:
        logger.warning("Telegram unreachable%s: %s", suffix, last)
    return None
