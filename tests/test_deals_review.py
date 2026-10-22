"""Regression tests for the independent review of deals & commission."""
import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.deals import DealError
from tests.test_deals_core import lead_status, make
from tests.test_followups_core import NOW, add_lead


def test_merging_two_leads_keeps_their_deals():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/capture", json={"name": "Sara", "phone": "0501234567", "consent": True})
    c.post("/capture", json={"name": "Sara", "email": "sara@example.com", "consent": True})
    ids = [x["id"] for x in c.get("/leads").json()]
    assert len(ids) == 2
    d = c.post("/deals", json={"lead_id": max(ids), "agent_id": 1, "kind": "sale", "value_aed": 1_000_000, "title": "Marina flat"}).json()
    c.post(f"/deals/{d['id']}/stage", json={"stage": "paid"})
    c.post("/capture", json={"name": "Sara", "phone": "0501234567", "email": "sara@example.com", "consent": True})        # links the two profiles
    assert len(c.get("/leads").json()) == 1
    got = c.get(f"/deals/{d['id']}").json()
    assert got["lead_id"] == c.get("/leads").json()[0]["id"] and got["title"] == "Marina flat"
    assert next(r for r in c.get("/roi").json() if r["revenue_aed"])["revenue_aed"] == 1_000_000


def test_creating_a_deal_at_a_won_stage_wins_the_lead_and_stops_follow_ups():
    db, d, fu = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    d.create(lead, 1, "sale", 1_000_000, stage="contract_signed")
    assert lead_status(db, lead) == "won" and db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='active'")["n"] == 0


def test_lead_status_follows_reversals():
    db, d, _ = make()
    lead = add_lead(db)
    deal = d.create(lead, 1, "sale", 1_000_000)
    d.move(deal["id"], "offer_accepted", "x")
    assert lead_status(db, lead) == "won"
    d.move(deal["id"], "lost", "x", lost_reason="fell through")
    assert lead_status(db, lead) == "lost"
    d.move(deal["id"], "offer_made", "x")
    assert lead_status(db, lead) == "qualified"                                  # a live deal again
    d.move(deal["id"], "paid", "x")
    d.move(deal["id"], "offer_made", "x")
    assert lead_status(db, lead) == "qualified"


def test_manual_status_without_deals_is_left_alone():
    db, d, _ = make()
    lead = add_lead(db, status="won")
    other = d.create(lead, 1, "sale", 1_000_000)
    d.move(other["id"], "offer_made", "x")
    assert lead_status(db, lead) == "qualified"                                  # the only deal is open, so 'won' (set earlier) no longer stands


def test_erasure_also_clears_the_lost_reason():
    db, d, _ = make()
    lead = add_lead(db)
    deal = d.create(lead, 1, "sale", 1_000_000)
    d.move(deal["id"], "lost", "x", lost_reason="Sara divorced, call +971501234567")
    with db.tx() as c:
        c.execute("DELETE FROM leads WHERE id=?", (lead,))
    assert d.get(deal["id"])["lost_reason"] is None


@pytest.mark.parametrize("bad", [10 ** 400, -(10 ** 400), 1e999, float("nan")])
def test_huge_numbers_are_a_clean_error(bad):
    db, d, _ = make()
    lead = add_lead(db)
    with pytest.raises(DealError):
        d.create(lead, 1, "sale", bad)
    with pytest.raises(DealError):
        d.create(lead, 1, "sale", 1_000_000, split_pct=bad)


def test_http_never_500s_on_hostile_ids_and_values():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/capture", json={"name": "Sara", "phone": "0501234567", "consent": True})
    lead = c.get("/leads").json()[0]["id"]
    big = '{"lead_id": 1' + "0" * 30 + ', "kind": "sale", "value_aed": 1000000}'
    assert c.post("/deals", content=big, headers={"content-type": "application/json"}).status_code == 422
    for agent in ({"a": 1}, "abc", True, 1.5):
        assert c.post("/deals", json={"lead_id": lead, "agent_id": agent, "kind": "sale", "value_aed": 1_000_000}).status_code == 422
    assert c.post("/deals", content='{"lead_id": %d, "kind": "sale", "value_aed": %s}' % (lead, "9" * 400), headers={"content-type": "application/json"}).status_code == 422


def test_dates_must_be_real_calendar_dates():
    db, d, _ = make()
    lead = add_lead(db)
    for bad in ("2026-99-99", "2026-02-30", "2026-10-22\n", "٢٠٢٦-١٠-٢٢"):
        with pytest.raises(DealError):
            d.create(lead, 1, "sale", 1_000_000, expected_close=bad)
    assert d.create(lead, 1, "sale", 1_000_000, expected_close="2026-10-22")["expected_close"] == "2026-10-22"


def test_patching_other_fields_does_not_reprice_old_deals():
    db, d, _ = make()
    deal = d.create(add_lead(db), 1, "sale", 2_000_000)
    d.put_settings({"vat_pct": 0})
    assert d.update(deal["id"], {"title": "renamed"}, "x")["vat_aed"] == 2_000
    assert d.update(deal["id"], {"value_aed": 3_000_000}, "x")["vat_aed"] == 0           # a price change reprices with the current VAT


def test_changing_kind_re_defaults_an_unedited_rate():
    db, d, _ = make()
    deal = d.create(add_lead(db), 1, "sale", 100_000)
    assert d.update(deal["id"], {"kind": "rental"}, "x")["commission_pct"] == 5
    custom = d.create(add_lead(db, phone="+971502222222", email="b@example.com"), 1, "sale", 100_000, commission_pct=1.5)
    assert d.update(custom["id"], {"kind": "rental"}, "x")["commission_pct"] == 1.5


def test_a_repeat_lost_move_updates_the_reason_and_open_deals_exclude_paid():
    db, d, _ = make()
    lead = add_lead(db)
    a, b = d.create(lead, 1, "sale", 1_000_000), d.create(lead, 1, "sale", 500_000)
    d.move(a["id"], "lost", "x", lost_reason="price")
    assert d.move(a["id"], "lost", "x", lost_reason="found cheaper")["lost_reason"] == "found cheaper"
    d.move(b["id"], "paid", "x")
    assert d.leaderboard()[0]["deals"] == 0


def test_months_use_dubai_time():
    from datetime import datetime, timezone
    db, d, _ = make()
    deal = d.create(add_lead(db), 1, "sale", 1_000_000)
    d.move(deal["id"], "paid", "x")
    with db.tx() as c:
        c.execute("UPDATE deal_events SET ts='2026-09-30T21:30:00+00:00' WHERE to_stage='paid'")       # 01:30 on 1 Oct in Dubai
    assert d.leaderboard(period="2026-10")[0]["closed_deals"] == 1 and d.leaderboard(period="2026-09") == []
    assert d.summary(now=datetime(2026, 10, 15, tzinfo=timezone.utc))["closed_this_month"]["count"] == 1
