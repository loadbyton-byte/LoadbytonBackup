"""Regression tests for the final whole-branch review (C-1, I-1 … I-8)."""
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.capture import RateLimiter
from leadengine.config import Settings
from leadengine.connectors.whatsapp import DryRunSender, Inbound, handle_inbound
from leadengine.db import DB
from leadengine.distribution import Distribution
from leadengine.inventory import import_projects_csv, parse_beds
from leadengine.models import Signal
from leadengine.qualify import is_stop
from leadengine.roi import record_spend, roi_report

OK = {"name": "A", "phone": "0501234567", "consent": True}
BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"


def app_client(**kw):
    return TestClient(create_app(Settings(db_path=":memory:", api_key=kw.pop("api_key", ""), **kw), DB(":memory:")))


# ---------------------------------------------------------------- C-1
def test_agent_weight_and_capacity_are_validated():
    c = app_client()
    assert c.post("/agents", json={"name": "A", "weight": 0}).status_code == 422
    assert c.post("/agents", json={"name": "A", "max_open": -1}).status_code == 422
    a = c.post("/agents", json={"name": "A"}).json()["id"]
    assert c.patch(f"/agents/{a}", json={"weight": 0}).status_code == 422
    assert c.patch(f"/agents/{a}", json={"weight": -3}).status_code == 422
    assert c.patch(f"/agents/{a}", json={"max_open": -1}).status_code == 422


def test_assignment_survives_a_zero_weight_already_in_the_database(pipe):
    d = Distribution(pipe.db)
    a = d.add_agent("A")
    with pipe.db.tx() as c:
        c.execute("UPDATE agents SET weight=0 WHERE id=?", (a,))
    lid = pipe.ingest(Signal(platform="reddit", external_id="x", text=BUY, author_handle="person_x_1")).lead_id
    assert d.assign(lid) == a


# ---------------------------------------------------------------- I-1
def test_whatsapp_renter_flow_writes_answers_onto_the_lead(pipe):
    s, n = DryRunSender(), [0]
    for text in ["Hello", "I'd like to rent", "JLT", "85k per year", "next month", "yes"]:
        n[0] += 1
        handle_inbound(pipe, s, Inbound("971501234567", "Aisha", text, f"m{n[0]}", 1_700_000_000 + n[0]))
    lead = pipe.db.one("SELECT * FROM leads")
    assert (lead["label"], lead["purchase_type"]) == ("renter", "rent")
    assert lead["budget_max"] == 85_000 and lead["primary_area"] == "Jumeirah Lake Towers" and lead["timeline_days"] == 30
    assert lead["consent_status"] == "opted_in"


# ---------------------------------------------------------------- I-2 (browser part lives in test_dashboard_smoke)
def test_status_and_activity_api_still_works_for_the_dashboard():
    c = app_client()
    c.post("/ingest/signals", json=[{"platform": "reddit", "external_id": "a", "text": BUY, "author_handle": "person_a_1"}])
    assert c.post("/leads/1/status", json={"status": "contacted"}).status_code == 200
    assert c.post("/leads/1/activities", json={"kind": "call"}).status_code == 200


# ---------------------------------------------------------------- I-3
def test_capture_rate_limit_is_not_bypassed_by_spoofed_forwarded_for():
    c = app_client(capture_origins=[])
    codes = [c.post("/capture", json=OK, headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code for i in range(8)]
    assert 429 in codes


def test_trusted_proxy_uses_the_hop_it_added():
    c = app_client(capture_origins=[], trusted_proxies=1)
    spoof = [c.post("/capture", json=OK, headers={"x-forwarded-for": f"6.6.6.6, 9.9.9.{i}"}).status_code for i in range(8)]
    assert 429 not in spoof                                            # distinct real clients behind the proxy
    same = [c.post("/capture", json=OK, headers={"x-forwarded-for": f"1.1.1.{i}, 8.8.8.8"}).status_code for i in range(8)]
    assert 429 in same                                                 # same real client, rotating spoofed prefix


def test_limiter_prunes_expired_keys_and_keeps_active_offenders():
    lim = RateLimiter(2, 60)
    for i in range(4000):
        lim.hit(f"old{i}", now=0.0)                                    # under capacity: nothing is evicted
    assert [lim.hit("bad", now=1000.0) for _ in range(3)] == [False, False, True]
    for i in range(1500):
        lim.hit(f"new{i}", now=1000.0)                                 # crosses capacity -> expired keys go, live keys stay
    assert "old0" not in lim._hits and "bad" in lim._hits and len(lim._hits) <= 1502
    assert lim.hit("bad", now=1001.0) is True                          # the offender was not forgotten


# ---------------------------------------------------------------- I-4
def test_ics_feed_uses_per_agent_tokens_not_the_master_key():
    c = app_client(api_key="master")
    H = {"x-api-key": "master"}
    a = c.post("/agents", json={"name": "A"}, headers=H).json()
    b = c.post("/agents", json={"name": "B"}, headers=H).json()
    assert a["feed_token"] and a["feed_token"] != b["feed_token"] and "master" not in a["feed_token"]
    assert c.get(f"/viewings.ics?agent_id={a['id']}&token={a['feed_token']}").status_code == 200
    assert c.get(f"/viewings.ics?agent_id={b['id']}&token={a['feed_token']}").status_code == 401      # other agent's feed
    assert c.get(f"/viewings.ics?token={a['feed_token']}").status_code == 401                          # no all-agents feed
    assert c.get("/viewings.ics?key=master").status_code == 401                                        # master key not accepted in URL
    assert c.get("/viewings.ics", headers=H).status_code == 200


# ---------------------------------------------------------------- I-5
@pytest.mark.parametrize("text", ["Unsubscribe me please", "stop pls", "توقف", "أوقف", "ＳＴＯＰ", "remove me", "Stop, please!", "من فضلك ايقاف"])
def test_more_opt_out_phrasings(text):
    assert is_stop(text)


@pytest.mark.parametrize("text", ["stop by tomorrow", "please call me tomorrow", "I can't stop thinking about the villa", "remove the sofa"])
def test_conversational_messages_are_not_opt_outs(text):
    assert not is_stop(text)


# ---------------------------------------------------------------- I-6
HEAD = "name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\n"


def test_unquoted_commas_row_is_skipped_not_misread(pipe):
    res = import_projects_csv(pipe.db, HEAD + "Good2,Dev,Business Bay,Dubai,AED 1,500,000,AED 2,000,000,2,2030,\n"
                                                  "Fine,Dev,Business Bay,Dubai,1.5M,2M,2,2030,\n")
    assert res["imported"] == 1 and len(res["skipped"]) == 1 and "column" in res["skipped"][0][1]


def test_capitalised_and_spaced_headers_are_accepted(pipe):
    res = import_projects_csv(pipe.db, "Name,Developer,Area,Emirate,Min Price,Max_Price,Bedrooms,Handover,Payment Plan\n"
                                       "Excel Tower,Dev,Dubai Marina,Dubai,1.2M,2M,2,2030,60/40\n")
    assert res["imported"] == 1 and not res["skipped"]


def test_reimport_updates_instead_of_duplicating(pipe):
    csv = HEAD + "Tower,Dev,Dubai Marina,Dubai,1.2M,2M,2,2030,60/40\n"
    import_projects_csv(pipe.db, csv)
    import_projects_csv(pipe.db, csv.replace("1.2M", "1.3M"))
    rows = pipe.db.q("SELECT min_price FROM projects")
    assert len(rows) == 1 and rows[0]["min_price"] == 1_300_000


def test_huge_bedroom_range_is_bounded_and_fast(pipe):
    t = time.time()
    res = import_projects_csv(pipe.db, HEAD + "Boom,Dev,Dubai Marina,Dubai,1.2M,2M,0-100000000,2030,\n")
    assert time.time() - t < 2 and parse_beds("0-100000000") == [] and parse_beds("1-3") == [1, 2, 3]
    assert res["imported"] == 0 and "bedrooms" in res["skipped"][0][1]


def test_prices_below_sanity_floor_are_rejected(pipe):
    res = import_projects_csv(pipe.db, HEAD + "Tiny,Dev,Dubai Marina,Dubai,10,500,2,2030,\n")
    assert res["imported"] == 0 and "price" in res["skipped"][0][1]


# ---------------------------------------------------------------- I-7
def test_roi_period_filters_leads_and_spend(pipe):
    a = pipe.ingest(Signal(platform="reddit", external_id="a", text=BUY, author_handle="person_a_1")).lead_id
    b = pipe.ingest(Signal(platform="reddit", external_id="b", text=BUY.replace("2BR", "3BR"), author_handle="person_b_1")).lead_id
    with pipe.db.tx() as c:
        c.execute("UPDATE leads SET first_seen='2020-01-05T10:00:00+00:00' WHERE id=?", (b,))
    cur = datetime.now(timezone.utc).strftime("%Y-%m")
    record_spend(pipe.db, "reddit", None, cur, 300)
    record_spend(pipe.db, "reddit", None, "2020-01", 900)
    one = next(r for r in roi_report(pipe.db, period=cur) if r["source"] == "reddit")
    assert (one["leads"], one["spend_aed"]) == (1, 300) and a
    allt = next(r for r in roi_report(pipe.db) if r["source"] == "reddit")
    assert (allt["leads"], allt["spend_aed"]) == (2, 1200)


def test_spend_source_names_are_normalised_to_the_lead_platform(pipe):
    pipe.ingest(Signal(platform="portal:bayut", external_id="p", source_type="portal_inquiry", text="is it available?", phone="0507654321"))
    record_spend(pipe.db, " Bayut ", None, "2026-10", 400)
    rep = roi_report(pipe.db)
    assert next(r for r in rep if r["source"] == "portal:bayut")["spend_aed"] == 400 and not any(r["source"] in ("bayut", "Bayut") for r in rep)


# ---------------------------------------------------------------- I-8
def test_agent_areas_are_canonicalised_so_abbreviations_route(pipe):
    d = Distribution(pipe.db)
    generalist = d.add_agent("Generalist")
    jvc = d.add_agent("JVC specialist", areas=("JVC",))
    assert json.loads(pipe.db.one("SELECT areas FROM agents WHERE id=?", (jvc,))["areas"]) == ["Jumeirah Village Circle"]
    lid = pipe.ingest(Signal(platform="reddit", external_id="j", text="Looking to buy in JVC, budget 1M, cash buyer, this week", author_handle="person_j_1")).lead_id
    assert pipe.db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (lid,))["assigned_agent_id"] == jvc != generalist


def test_patching_agent_areas_canonicalises_too():
    c = app_client()
    a = c.post("/agents", json={"name": "A"}).json()["id"]
    assert c.patch(f"/agents/{a}", json={"areas": ["JVC", "Marina"]}).json()["areas"] == ["Jumeirah Village Circle", "Dubai Marina"]
