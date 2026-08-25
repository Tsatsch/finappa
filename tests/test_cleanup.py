from datetime import datetime, timezone

import pytest

from app.categories import CategoryName
from app.categorize import resolve_category
from app.cleanup import (
    CleanupError,
    CleanupProposal,
    apply_merge,
    filter_proposals,
    reject_proposal,
    start_cleanup_session,
    undo_last_cleanup_session,
)
from app.db import (
    Category,
    CleanupSession,
    Merchant,
    MerchantAlias,
    Transaction,
    get_sessionmaker,
    init_db,
    make_engine,
)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def _two_rewe_variants(session):
    rewe, groceries = resolve_category(session, "Rewe", CategoryName.GROCERIES)
    city, _ = resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    now = datetime.now(timezone.utc)
    session.add(
        Transaction(
            ts=now,
            amount_cents=500,
            type="EXPENSE",
            merchant_id=city.id,
            category_id=groceries.id,
            source="message",
        )
    )
    session.commit()
    return rewe, city, groceries


class _FakeCompletion:
    def __init__(self, parsed):
        message = type("Message", (), {"parsed": parsed})()
        self.choices = [type("Choice", (), {"message": message})()]


def test_propose_merges_filters_model_output(session, monkeypatch):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()

    from app import cleanup as cleanup_mod

    def fake_parse(**kwargs):
        assert kwargs["model"] == "gpt-5.6-terra"
        assert kwargs["reasoning_effort"] == "medium"
        parsed = cleanup_mod.CleanupProposeResult(
            proposals=[
                cleanup_mod.CleanupProposeItem(
                    from_name="Rewe City", to_name="Rewe", reason="variant"
                ),
                cleanup_mod.CleanupProposeItem(
                    from_name="Cafe", to_name="Rewe", reason="bad"
                ),
            ]
        )
        return _FakeCompletion(parsed)

    monkeypatch.setattr(cleanup_mod, "_cleanup_parse", fake_parse)

    proposals = cleanup_mod.propose_merges(
        type("Client", (), {})(),
        "gpt-5.6-terra",
        "medium",
        session,
        max_proposals=20,
    )

    assert len(proposals) == 1
    assert proposals[0].from_name == "Rewe City"


def test_filter_drops_cross_category(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()
    kept = filter_proposals(
        session,
        [
            CleanupProposal("Cafe", "Rewe", "nope"),
            CleanupProposal("Rewe City", "Rewe", "variant"),
        ],
    )
    assert kept == []


def test_filter_keeps_same_category_pair(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    kept = filter_proposals(
        session, [CleanupProposal("Rewe City", "Rewe", "variant")]
    )
    assert len(kept) == 1
    assert kept[0].from_name == "Rewe City"


def test_apply_merge_moves_txs_keeps_category_creates_alias(session):
    rewe, city, groceries = _two_rewe_variants(session)
    dining = session.query(Category).filter_by(name="Dining").one()
    tx = session.query(Transaction).filter_by(merchant_id=city.id).one()
    tx.category_id = dining.id
    session.commit()

    sess = start_cleanup_session(session)
    msg = apply_merge(session, sess.id, "Rewe City", "Rewe")

    assert "Merged" in msg
    assert "Rewe City" in msg and "Rewe" in msg
    assert session.get(Merchant, city.id) is None
    assert (
        session.query(MerchantAlias)
        .filter_by(alias_normalized="rewe city")
        .one()
        .merchant_id
        == rewe.id
    )
    tx = session.query(Transaction).one()
    assert tx.merchant_id == rewe.id
    assert tx.category_id == dining.id


def test_apply_merge_rejects_cross_category(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Cafe", CategoryName.DINING)
    session.commit()
    sess = start_cleanup_session(session)
    with pytest.raises(CleanupError, match="categor"):
        apply_merge(session, sess.id, "Cafe", "Rewe")


def test_reject_then_filter_excludes_pair(session):
    resolve_category(session, "Rewe", CategoryName.GROCERIES)
    resolve_category(session, "Rewe City", CategoryName.GROCERIES)
    session.commit()
    reject_proposal(session, "Rewe City", "Rewe")
    kept = filter_proposals(
        session, [CleanupProposal("Rewe City", "Rewe", "variant")]
    )
    assert kept == []


def test_undo_restores_absorbed_merchant_and_txs(session):
    rewe, city, groceries = _two_rewe_variants(session)
    sess = start_cleanup_session(session)
    apply_merge(session, sess.id, "Rewe City", "Rewe")

    msg = undo_last_cleanup_session(session)
    assert "undo" in msg.lower() or "restored" in msg.lower()

    restored = session.query(Merchant).filter_by(name="rewe city").one()
    assert restored.display_name == "Rewe City"
    assert restored.category_id == groceries.id
    assert (
        session.query(MerchantAlias).filter_by(alias_normalized="rewe city").count()
        == 0
    )
    tx = session.query(Transaction).one()
    assert tx.merchant_id == restored.id
    assert session.get(CleanupSession, sess.id).undone_at is not None


def test_undo_when_nothing_raises(session):
    with pytest.raises(CleanupError):
        undo_last_cleanup_session(session)
