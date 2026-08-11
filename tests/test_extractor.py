from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.categories import CategoryName
from app.extractor import (
    ExtractionError,
    ExtractionResult,
    TransactionItem,
    extract_transactions,
    validate_extraction,
)


def _result(*items: TransactionItem) -> ExtractionResult:
    return ExtractionResult(transactions=list(items))


def _item(**overrides) -> TransactionItem:
    defaults = dict(
        amount=30.0,
        currency="EUR",
        merchant="Rewe",
        category=CategoryName.GROCERIES,
        category_confident=True,
        type="EXPENSE",
        notes=None,
    )
    defaults.update(overrides)
    return TransactionItem(**defaults)


def test_transaction_item_requires_category_confident():
    with pytest.raises(ValidationError):
        TransactionItem(
            amount=30.0, currency="EUR", merchant="Rewe",
            category=CategoryName.GROCERIES, type="EXPENSE", notes=None,
        )


def test_transaction_item_accepts_category_confident_true_and_false():
    confident = _item(category_confident=True)
    unconfident = _item(category_confident=False)
    assert confident.category_confident is True
    assert unconfident.category_confident is False


def test_validate_extraction_passes_valid_items():
    items = validate_extraction(_result(_item()))
    assert len(items) == 1
    assert items[0].merchant == "Rewe"


def test_validate_extraction_rejects_non_eur():
    with pytest.raises(ExtractionError, match="EUR"):
        validate_extraction(_result(_item(currency="USD")))


def test_validate_extraction_rejects_non_positive_amount():
    with pytest.raises(ExtractionError):
        validate_extraction(_result(_item(amount=0)))


def test_validate_extraction_rejects_absurd_amount():
    with pytest.raises(ExtractionError):
        validate_extraction(_result(_item(amount=2_000_000)))


def test_validate_extraction_rejects_empty_result():
    with pytest.raises(ExtractionError, match="didn't find"):
        validate_extraction(_result())


def test_extract_transactions_wraps_api_failure():
    client = MagicMock()
    client.chat.completions.parse.side_effect = TimeoutError("boom")
    with pytest.raises(ExtractionError, match="Couldn't reach"):
        extract_transactions(client, "gpt-4o-mini", "30€ Rewe")


def test_extract_transactions_returns_items_on_success():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=_result(_item())))]
    client.chat.completions.parse.return_value = completion
    items = extract_transactions(client, "gpt-4o-mini", "30€ Rewe")
    assert items[0].merchant == "Rewe"


def test_extract_transactions_wraps_refusal_response():
    """When OpenAI returns a refusal (parsed=None), extract_transactions should wrap
    the resulting validation error as ExtractionError, not let AttributeError escape."""
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=None))]
    client.chat.completions.parse.return_value = completion
    with pytest.raises(ExtractionError):
        extract_transactions(client, "gpt-4o-mini", "some text")
