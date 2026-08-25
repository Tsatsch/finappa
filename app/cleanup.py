import json
from dataclasses import dataclass
from datetime import datetime, timezone

from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.categorize import normalize_merchant_name
from app.db import (
    Category,
    CleanupRejection,
    CleanupSession,
    CleanupSessionMerge,
    Merchant,
    MerchantAlias,
    Transaction,
)


class CleanupError(Exception):
    """User-facing cleanup error."""


@dataclass
class CleanupProposal:
    from_name: str
    to_name: str
    reason: str


def format_cleanup_prompt(
    proposal: CleanupProposal, index: int, total: int
) -> str:
    return (
        f"Cleanup {index + 1}/{total}\n"
        f"{proposal.from_name} → {proposal.to_name}\n"
        f"{proposal.reason}"
    )


class CleanupProposeItem(BaseModel):
    from_name: str
    to_name: str
    reason: str


class CleanupProposeResult(BaseModel):
    proposals: list[CleanupProposeItem]


CLEANUP_SYSTEM_PROMPT = (
    "Identify merchant names that are aliases or variants of the same merchant or "
    "brand. Never merge peer chains or competitors (for example, Rewe and Edeka). "
    "Only suggest pairs from the provided merchant list."
)


def _cleanup_parse(
    client: OpenAI,
    model: str,
    reasoning_effort: str,
    messages: list[dict[str, str]],
    response_format: type[BaseModel],
):
    parse = getattr(client.chat.completions, "parse", None)
    if parse is None:  # pragma: no cover - depends on installed openai-python version
        parse = client.beta.chat.completions.parse
    kwargs = {
        "model": model,
        "messages": messages,
        "response_format": response_format,
        "reasoning_effort": reasoning_effort,
    }
    try:
        return parse(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort")
        return parse(**kwargs)


def propose_merges(
    client: OpenAI,
    model: str,
    reasoning_effort: str,
    session: Session,
    *,
    max_proposals: int = 20,
) -> list[CleanupProposal]:
    merchants = (
        session.query(
            Merchant.display_name,
            Merchant.name,
            Category.name,
            func.count(Transaction.id),
        )
        .join(Category, Merchant.category_id == Category.id)
        .outerjoin(Transaction, Transaction.merchant_id == Merchant.id)
        .group_by(Merchant.id, Category.name)
        .all()
    )
    payload = [
        {
            "display_name": display_name,
            "normalized_name": normalized_name,
            "category": category_name,
            "transaction_count": transaction_count,
        }
        for display_name, normalized_name, category_name, transaction_count in merchants
    ]
    completion = _cleanup_parse(
        client=client,
        model=model,
        reasoning_effort=reasoning_effort,
        messages=[
            {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format=CleanupProposeResult,
    )
    parsed = completion.choices[0].message.parsed
    proposals = [
        CleanupProposal(item.from_name, item.to_name, item.reason)
        for item in parsed.proposals
    ]
    return filter_proposals(session, proposals)[:max_proposals]


def rejection_pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((normalize_merchant_name(a), normalize_merchant_name(b))))


def _resolve_merchant(session: Session, name: str) -> Merchant | None:
    normalized = normalize_merchant_name(name)
    merchant = session.query(Merchant).filter_by(name=normalized).one_or_none()
    if merchant is not None:
        return merchant
    return (
        session.query(Merchant)
        .filter(func.lower(Merchant.display_name) == normalized)
        .one_or_none()
    )


def filter_proposals(
    session: Session, proposals: list[CleanupProposal]
) -> list[CleanupProposal]:
    kept = []
    seen_pairs: set[tuple[int, int]] = set()
    for proposal in proposals:
        absorbed = _resolve_merchant(session, proposal.from_name)
        canonical = _resolve_merchant(session, proposal.to_name)
        if (
            absorbed is None
            or canonical is None
            or absorbed.id == canonical.id
            or absorbed.category_id != canonical.category_id
        ):
            continue
        merchant_pair = tuple(sorted((absorbed.id, canonical.id)))
        if merchant_pair in seen_pairs:
            continue
        left, right = rejection_pair(proposal.from_name, proposal.to_name)
        rejected = (
            session.query(CleanupRejection)
            .filter_by(left_normalized=left, right_normalized=right)
            .first()
        )
        if rejected is None:
            kept.append(proposal)
            seen_pairs.add(merchant_pair)
    return kept


def start_cleanup_session(session: Session) -> CleanupSession:
    cleanup_session = CleanupSession()
    session.add(cleanup_session)
    session.commit()
    return cleanup_session


def apply_merge(
    session: Session, session_id: int, from_name: str, to_name: str
) -> str:
    cleanup_session = session.get(CleanupSession, session_id)
    if cleanup_session is None or cleanup_session.undone_at is not None:
        raise CleanupError("Cleanup session is not active.")

    absorbed = _resolve_merchant(session, from_name)
    canonical = _resolve_merchant(session, to_name)
    if absorbed is None or canonical is None:
        raise CleanupError("One or both merchants no longer exist.")
    if absorbed.id == canonical.id:
        raise CleanupError("Cannot merge a merchant into itself.")
    if absorbed.category_id != canonical.category_id:
        raise CleanupError("Cannot merge merchants from different categories.")

    transaction_ids = [
        tx_id
        for (tx_id,) in session.query(Transaction.id)
        .filter_by(merchant_id=absorbed.id)
        .all()
    ]
    session.query(Transaction).filter_by(merchant_id=absorbed.id).update(
        {"merchant_id": canonical.id}, synchronize_session="fetch"
    )
    session.query(MerchantAlias).filter_by(merchant_id=absorbed.id).update(
        {"merchant_id": canonical.id}, synchronize_session="fetch"
    )
    session.add(
        MerchantAlias(
            alias_normalized=absorbed.name,
            merchant_id=canonical.id,
        )
    )
    session.add(
        CleanupSessionMerge(
            session_id=session_id,
            canonical_merchant_id=canonical.id,
            absorbed_normalized=absorbed.name,
            absorbed_display_name=absorbed.display_name,
            absorbed_category_id=absorbed.category_id,
            alias_normalized=absorbed.name,
            tx_ids_json=json.dumps(transaction_ids),
        )
    )
    display_from = absorbed.display_name
    display_to = canonical.display_name
    session.delete(absorbed)
    session.commit()
    return (
        f"Merged {display_from} → {display_to} "
        f"({len(transaction_ids)} transactions)."
    )


def reject_proposal(session: Session, from_name: str, to_name: str) -> None:
    left, right = rejection_pair(from_name, to_name)
    existing = (
        session.query(CleanupRejection)
        .filter_by(left_normalized=left, right_normalized=right)
        .first()
    )
    if existing is None:
        session.add(
            CleanupRejection(left_normalized=left, right_normalized=right)
        )
    session.commit()


def undo_last_cleanup_session(session: Session) -> str:
    cleanup_session = (
        session.query(CleanupSession)
        .join(
            CleanupSessionMerge,
            CleanupSessionMerge.session_id == CleanupSession.id,
        )
        .filter(CleanupSession.undone_at.is_(None))
        .order_by(CleanupSession.id.desc())
        .first()
    )
    if cleanup_session is None:
        raise CleanupError("There is no cleanup session to undo.")

    merges = (
        session.query(CleanupSessionMerge)
        .filter_by(session_id=cleanup_session.id)
        .order_by(CleanupSessionMerge.id.desc())
        .all()
    )
    for merge in merges:
        restored = Merchant(
            name=merge.absorbed_normalized,
            display_name=merge.absorbed_display_name,
            category_id=merge.absorbed_category_id,
        )
        session.add(restored)
        session.flush()
        transaction_ids = json.loads(merge.tx_ids_json)
        if transaction_ids:
            session.query(Transaction).filter(
                Transaction.id.in_(transaction_ids)
            ).update({"merchant_id": restored.id}, synchronize_session="fetch")
        session.query(MerchantAlias).filter_by(
            alias_normalized=merge.alias_normalized
        ).delete(synchronize_session="fetch")

    cleanup_session.undone_at = datetime.now(timezone.utc)
    session.commit()
    return f"Restored {len(merges)} merchant merges."
