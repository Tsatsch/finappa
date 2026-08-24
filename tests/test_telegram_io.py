from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from app.telegram_io import telegram_send


async def test_telegram_send_retries_then_succeeds():
    send = AsyncMock(side_effect=[NetworkError("drop"), NetworkError("drop"), "ok"])

    result = await telegram_send(send, retries=3)

    assert result == "ok"
    assert send.await_count == 3


async def test_telegram_send_logs_warning_when_not_ledger(caplog):
    send = AsyncMock(side_effect=NetworkError("drop"))

    result = await telegram_send(send, retries=1, context="/summary")

    assert result is None
    assert any("Telegram unreachable (/summary)" in r.message for r in caplog.records)
    assert not any("Ledger changed" in r.message for r in caplog.records)


async def test_telegram_send_logs_error_when_ledger_written(caplog):
    send = AsyncMock(side_effect=NetworkError("drop"))

    result = await telegram_send(
        send, retries=1, ledger_written=True, context="update_id=42"
    )

    assert result is None
    assert any(
        "Ledger changed but Telegram unreachable (update_id=42)" in r.message
        for r in caplog.records
    )
