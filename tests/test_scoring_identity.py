from datetime import timedelta

from leadengine.identity import normalize_email, normalize_handle, normalize_phone
from leadengine.models import utcnow
from leadengine.nlp.intent import classify
from leadengine.scoring import score_signal


def test_phone_normalisation():
    for raw in ("0501234567", "+971 50 123 4567", "00971501234567", "501234567", "971501234567"):
        assert normalize_phone(raw) == "+971501234567", raw
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("12345") is None and normalize_phone(None) is None
    assert normalize_email(" A@B.com ") == "a@b.com" and normalize_email("nope") is None
    assert normalize_handle("@U/Foo") == "foo"


def _s(text, **kw):
    it = classify(text)
    return score_signal(it, kw.pop("source", "public_post"), kw.pop("at", utcnow()), kw.pop("phone", False), False, False)


def test_ready_specific_buyer_outranks_vague_researcher():
    hot = _s("Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week, recommend an agent")
    cold = _s("thinking about buying property someday, should i?")
    assert hot.tier == "HOT" and cold.total < hot.total - 30
    assert hot.sla_due_at is not None


def test_recency_decay():
    fresh = _s("Looking to buy a 2BR in Dubai Marina budget 1.8M")
    stale = _s("Looking to buy a 2BR in Dubai Marina budget 1.8M", at=utcnow() - timedelta(days=45))
    assert stale.total < fresh.total * 0.6


def test_first_party_form_beats_public_post_same_text():
    t = "Looking to buy a 2BR in Dubai Marina budget 1.8M"
    assert _s(t, source="lead_form", phone=True).total > _s(t).total


def test_unrealistic_budget_flagged():
    sc = _s("Looking to buy a villa in Dubai Hills, budget 200k")
    assert any("below typical floor" in f for f in sc.breakdown["flags"])


def test_noise_scores_zero():
    assert _s("Exclusive launch! Limited units, book now. BRN 12345").total == 0
