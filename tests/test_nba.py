from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.compliance import GST
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.distribution import Distribution
from leadengine.inventory import import_projects_csv
from leadengine.models import Signal
from leadengine.nba import next_actions, todays_tasks

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=GST)           # inside permitted contact hours
BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"


def mk(pipe, n=1, text=BUY, optin=False, **cols):
    if optin:
        lid = pipe.ingest(Signal(platform="facebook", external_id=f"f{n}", source_type="lead_form", text=text,
                                 phone=f"05012345{n:02d}", consent_marketing=True, consent_text="I agree")).lead_id
    else:
        lid = pipe.ingest(Signal(platform="reddit", external_id=f"r{n}", text=text, author_handle=f"person_{n}_x")).lead_id
    if cols:
        with pipe.db.tx() as c:
            for k, v in cols.items():
                c.execute(f"UPDATE leads SET {k}=? WHERE id=?", (v, lid))
    return lid


def act(pipe, lid, kind, ts):
    with pipe.db.tx() as c:
        c.execute("INSERT INTO activities(lead_id,ts,kind) VALUES(?,?,?)", (lid, ts.isoformat(), kind))


def acts(pipe, lid, now=NOW):
    row = pipe.db.one("SELECT * FROM leads WHERE id=?", (lid,))
    lead = dict(row)
    import json
    lead["platforms"] = json.loads(lead["platforms"])
    return {a["action"]: a for a in next_actions(pipe.db, pipe.compliance, lead, now)}


def test_unassigned_hot_lead_gets_assign_first(pipe):
    lid = mk(pipe)
    out = next_actions(pipe.db, pipe.compliance, {**dict(pipe.db.one("SELECT * FROM leads")), "platforms": ["reddit"]}, NOW)
    assert out[0]["action"] == "assign" and out[0]["allowed"] is True and lid


def test_overdue_new_lead_gets_call_and_blocked_channel_is_marked(pipe):
    lid = mk(pipe, assigned_agent_id=1, sla_due_at=(NOW - timedelta(hours=1)).isoformat())
    call = acts(pipe, lid)["call"]
    assert call["channel"] == "call" and call["allowed"] is False and "opt-in" in call["compliance_reason"] and "overdue" in call["reason"].lower()


def test_overdue_optin_lead_call_allowed_in_hours(pipe):
    lid = mk(pipe, optin=True, assigned_agent_id=1, sla_due_at=(NOW - timedelta(hours=1)).isoformat())
    assert acts(pipe, lid)["call"]["allowed"] is True


def test_overdue_call_outside_hours_is_deferred_to_next_window(pipe):
    lid = mk(pipe, optin=True, assigned_agent_id=1, sla_due_at=(NOW - timedelta(hours=1)).isoformat())
    late = NOW.replace(hour=22)
    call = acts(pipe, lid, now=late)["call"]
    assert call["allowed"] is False and datetime.fromisoformat(call["due"]).astimezone(GST).hour == 9


def test_not_yet_due_new_lead_gets_first_response(pipe):
    lid = mk(pipe, assigned_agent_id=1, sla_due_at=(NOW + timedelta(minutes=3)).isoformat())
    a = acts(pipe, lid)
    assert "call" not in a and a["first_response"]["channel"] == "public_reply" and a["first_response"]["allowed"] is True


def test_send_matches_when_inventory_fits(pipe):
    import_projects_csv(pipe.db, "name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\n"
                                 "Marina Edge,Acme,Dubai Marina,Dubai,1.2M,2.4M,\"1-3\",2030,60/40\n")
    lid = mk(pipe, optin=True, assigned_agent_id=1, status="contacted")
    m = acts(pipe, lid)["send_matches"]
    assert m["channel"] == "whatsapp" and m["allowed"] is True and "Marina Edge" in m["reason"]


def test_nurture_cadence_stops_after_three_then_reengage(pipe):
    lid = mk(pipe, optin=True, assigned_agent_id=1, status="contacted")
    t0 = NOW - timedelta(days=40)
    act(pipe, lid, "status:contacted", t0)
    assert "follow_up" not in acts(pipe, lid, now=t0 + timedelta(days=1))
    assert "follow_up" in acts(pipe, lid, now=t0 + timedelta(days=2, hours=1))
    act(pipe, lid, "follow_up", t0 + timedelta(days=2, hours=2))
    assert "follow_up" not in acts(pipe, lid, now=t0 + timedelta(days=3))
    assert "follow_up" in acts(pipe, lid, now=t0 + timedelta(days=5, hours=1))
    act(pipe, lid, "follow_up", t0 + timedelta(days=5, hours=2))
    assert "follow_up" in acts(pipe, lid, now=t0 + timedelta(days=10, hours=1))
    act(pipe, lid, "follow_up", t0 + timedelta(days=10, hours=2))
    late = acts(pipe, lid, now=t0 + timedelta(days=40))
    assert "follow_up" not in late and "reengage" in late


def test_seller_gets_valuation_visit(pipe):
    lid = mk(pipe, text="I want to sell my 2 bed apartment in Business Bay, how much is it worth?", assigned_agent_id=1, status="contacted")
    assert acts(pipe, lid)["book_valuation_visit"]["channel"] in ("call", "whatsapp")


def test_reengage_after_14_quiet_days(pipe):
    lid = mk(pipe, assigned_agent_id=1, status="qualified")
    act(pipe, lid, "status:qualified", NOW - timedelta(days=15))
    assert "reengage" in acts(pipe, lid)
    act(pipe, lid, "note", NOW - timedelta(days=2))
    assert "reengage" not in acts(pipe, lid)


def test_closed_leads_get_no_actions(pipe):
    lid = mk(pipe, assigned_agent_id=1, status="won")
    assert acts(pipe, lid) == {}


def test_tasks_sorted_hot_first_and_filtered_by_agent(pipe):
    d = Distribution(pipe.db)
    a, b = d.add_agent("A"), d.add_agent("B")
    hot = mk(pipe, 1, assigned_agent_id=a, sla_due_at=(NOW - timedelta(hours=1)).isoformat())
    warm = mk(pipe, 2, text="Looking to buy in JVC, budget 1M", assigned_agent_id=a, sla_due_at=(NOW - timedelta(hours=2)).isoformat())
    other = mk(pipe, 3, assigned_agent_id=b, sla_due_at=(NOW - timedelta(hours=1)).isoformat())
    tasks = todays_tasks(pipe.db, pipe.compliance, a, NOW)
    assert {t["lead_id"] for t in tasks} == {hot, warm} and other not in {t["lead_id"] for t in tasks}
    tiers = [t["tier"] for t in tasks]
    assert tiers == sorted(tiers, key={"HOT": 0, "WARM": 1, "NURTURE": 2}.get)


def test_api_endpoints():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/demo/seed")
    lid = c.get("/leads?tier=HOT").json()[0]["id"]
    assert c.get(f"/leads/{lid}/next-actions").json()
    assert c.get("/leads/9999/next-actions").status_code == 404
    assert c.get("/tasks/today").json()
    assert c.post(f"/leads/{lid}/activities", json={"kind": "follow_up", "channel": "whatsapp", "note": "sent matches"}).status_code == 200
    assert c.post(f"/leads/{lid}/activities", json={"kind": "drop table"}).status_code == 400
    assert c.get(f"/leads/{lid}").json()["activities"][0]["kind"] == "follow_up"
