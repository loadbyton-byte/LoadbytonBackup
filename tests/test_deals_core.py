import math
from datetime import datetime, timezone

import pytest

from leadengine.compliance import Compliance
from leadengine.config import Settings
from leadengine.connectors.whatsapp import DryRunSender
from leadengine.db import DB
from leadengine.deals import Deals, DealError, STAGES
from leadengine.followups import Followups
from leadengine.platform.mailer import ConsoleMailer
from tests.test_followups_core import NOW, add_lead


def make():
    db, s = DB(":memory:"), Settings(db_path=":memory:", api_key="")
    fu = Followups(db, Compliance(db, s), s, DryRunSender(), ConsoleMailer())
    return db, Deals(db, fu), fu


def lead_status(db, lead):
    return db.one("SELECT status FROM leads WHERE id=?", (lead,))["status"]


def test_commission_and_vat_follow_value_pct_and_split():
    db, d, _ = make()
    lead = add_lead(db)
    deal = d.create(lead, 1, "sale", 2_000_000)
    assert deal["commission_pct"] == 2 and deal["commission_aed"] == 40_000 and deal["vat_aed"] == 2_000
    rent = d.create(lead, 1, "rental", 100_000)
    assert rent["commission_pct"] == 5 and rent["commission_aed"] == 5_000
    upd = d.update(deal["id"], {"value_aed": 3_000_000, "split_pct": 50}, "api")
    assert upd["commission_aed"] == 30_000 and upd["vat_aed"] == 1_500 and upd["split_pct"] == 50
    assert d.update(deal["id"], {"commission_pct": 1.5}, "api")["commission_aed"] == 22_500


@pytest.mark.parametrize("kw", [
    {"value_aed": 0}, {"value_aed": -5}, {"value_aed": math.nan}, {"value_aed": math.inf}, {"value_aed": 1e11}, {"value_aed": True},
    {"value_aed": "100"}, {"split_pct": 101}, {"split_pct": -1}, {"commission_pct": 101}, {"kind": "barter"}, {"stage": "nope"},
    {"expected_close": "tomorrow"}, {"title": "x" * 300},
])
def test_bad_input_is_rejected_and_nothing_is_stored(kw):
    db, d, _ = make()
    lead = add_lead(db)
    args = {"lead_id": lead, "agent_id": 1, "kind": "sale", "value_aed": 1_000_000, **kw}
    with pytest.raises(DealError):
        d.create(**args)
    assert db.one("SELECT COUNT(*) n FROM deals")["n"] == 0


def test_unknown_lead_is_rejected():
    _, d, _ = make()
    with pytest.raises(DealError):
        d.create(999, 1, "sale", 1_000_000)


def test_stage_moves_are_audited_and_lost_needs_a_reason():
    db, d, _ = make()
    deal = d.create(add_lead(db), 1, "sale", 1_000_000)
    d.move(deal["id"], "offer_made", "ag")
    ev = d.get(deal["id"])["events"]
    assert [(e["from_stage"], e["to_stage"], e["actor"]) for e in ev][-1] == ("viewing_done", "offer_made", "ag")
    with pytest.raises(DealError):
        d.move(deal["id"], "lost", "ag")
    with pytest.raises(DealError):
        d.move(deal["id"], "bogus", "ag")
    assert d.move(deal["id"], "lost", "ag", lost_reason="bought elsewhere")["lost_reason"] == "bought elsewhere"


def test_accepting_an_offer_wins_the_lead_and_stops_follow_ups():
    db, d, fu = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    deal = d.create(lead, 1, "sale", 1_000_000)
    d.move(deal["id"], "offer_made", "x")
    assert lead_status(db, lead) == "new"
    d.move(deal["id"], "offer_accepted", "x")
    assert lead_status(db, lead) == "won"
    assert db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='active'")["n"] == 0
    assert fu._paused(lead)


def test_lost_deal_closes_the_lead_only_without_another_open_deal():
    db, d, fu = make()
    lead = add_lead(db)
    a, b = d.create(lead, 1, "sale", 1_000_000), d.create(lead, 1, "sale", 900_000)
    d.move(a["id"], "lost", "x", lost_reason="price")
    assert lead_status(db, lead) == "new"                                         # b is still open
    d.move(b["id"], "lost", "x", lost_reason="price")
    assert lead_status(db, lead) == "lost"


def test_reopening_a_lost_deal_does_not_restart_follow_ups():
    db, d, fu = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    deal = d.create(lead, 1, "sale", 1_000_000)
    d.move(deal["id"], "lost", "x", lost_reason="price")
    d.move(deal["id"], "viewing_done", "x")
    assert db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='active'")["n"] == 0 and d.get(deal["id"])["lost_reason"] is None


def test_summary_sums_open_pipeline_and_expected_commission():
    db, d, _ = make()
    l1, l2 = add_lead(db), add_lead(db, phone="+971502222222", email="b@example.com")
    d.create(l1, 1, "sale", 2_000_000)
    d.create(l2, 2, "sale", 1_000_000, stage="offer_made")
    paid = d.create(l2, 2, "sale", 500_000, stage="viewing_done")
    d.move(paid["id"], "paid", "x")
    lost = d.create(l1, 1, "sale", 700_000)
    d.move(lost["id"], "lost", "x", lost_reason="n/a")
    s = d.summary(now=datetime.now(timezone.utc))
    assert s["pipeline_value_aed"] == 3_000_000 and s["expected_commission_aed"] == 60_000 and s["by_stage"]["offer_made"]["count"] == 1
    assert s["closed_this_month"]["count"] == 1 and s["closed_this_month"]["commission_aed"] == 10_000
    assert d.summary(agent_id=1)["pipeline_value_aed"] == 2_000_000


def test_leaderboard_ranks_agents_and_filters_by_period():
    db, d, _ = make()
    l1, l2 = add_lead(db), add_lead(db, phone="+971502222222", email="b@example.com")
    for lead, agent, value in ((l1, 1, 2_000_000), (l2, 2, 1_000_000)):
        x = d.create(lead, agent, "sale", value)
        d.move(x["id"], "paid", "x")
    board = d.leaderboard()
    assert [r["agent_id"] for r in board] == [1, 2] and board[0]["closed_value_aed"] == 2_000_000 and board[0]["commission_aed"] == 40_000
    this = datetime.now(timezone.utc).strftime("%Y-%m")
    assert len(d.leaderboard(period=this)) == 2 and d.leaderboard(period="2001-01") == []
    assert [r["agent_id"] for r in d.leaderboard(agent_id=2)] == [2]


def test_erasing_a_lead_keeps_the_deal_but_drops_personal_text():
    db, d, _ = make()
    lead = add_lead(db)
    deal = d.create(lead, 1, "sale", 1_000_000, title="Sara's flat", notes="her mother's number")
    with db.tx() as c:
        c.execute("DELETE FROM leads WHERE id=?", (lead,))
    got = d.get(deal["id"])
    assert got["lead_id"] is None and got["notes"] is None and got["title"] is None and got["value_aed"] == 1_000_000


def test_settings_roundtrip_and_validation():
    _, d, _ = make()
    assert d.settings() == {"commission_sale_pct": 2.0, "commission_rental_pct": 5.0, "vat_pct": 5.0}
    assert d.put_settings({"commission_sale_pct": 2.5})["commission_sale_pct"] == 2.5
    for bad in ({"commission_sale_pct": 101}, {"vat_pct": -1}, {"nonsense": 1}, {"vat_pct": "x"}):
        with pytest.raises(DealError):
            d.put_settings(bad)
