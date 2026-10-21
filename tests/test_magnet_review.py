"""Regression tests for the lead-magnet checkpoint review."""
import re

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.magnets import forms
from leadengine.magnets.pages import csp
from leadengine.nlp.intent import parse_timeline
from leadengine.nlp.text import normalize

from test_concierge import fields, leads, start, step


def client(**kw):
    return TestClient(create_app(Settings(db_path=":memory:", api_key="", **kw), DB(":memory:")))


def form(c, path, **data):
    return c.post(path, data=data)


@pytest.mark.parametrize("tool,data", [
    ("mortgage", {"principal": "5", "rate": "1000", "years": "5"}), ("mortgage", {"principal": "1500000", "rate": "10000000000", "years": "5"}),
    ("mortgage", {"principal": "1500000", "rate": "4.5", "years": "1000"}), ("mortgage", {"principal": "1500000", "rate": "4.5", "years": "0"}),
    ("yield", {"price": "1500000", "rent": "100000", "service": "10000000000", "vacancy": "5"}), ("yield", {"price": "1500000", "rent": "100000", "service": "1", "vacancy": "150"}),
    ("yield", {"price": "100", "rent": "100000", "service": "0", "vacancy": "5"}), ("costs", {"price": "1500000", "loan": "9999999999"}),
    ("affordability", {"income": "30000", "years": "36", "rate": "4.5"})])
def test_every_tool_enforces_sane_ranges_on_page_and_lead(tool, data):
    c = client()
    assert c.post(f"/p/{tool}", data=data).status_code == 422
    assert c.post("/p/lead", data={"tool": tool, **data, "name": "X", "phone": "0501234567", "consent": "1"}).status_code == 422
    assert c.get("/leads").json() == []


def test_valid_extremes_still_work():
    c = client()
    assert c.post("/p/mortgage", data={"principal": "1500000", "rate": "0", "years": "35"}).status_code == 200
    assert c.post("/p/mortgage", data={"principal": "1500000", "rate": "30", "years": "1"}).status_code == 200
    assert c.post("/p/yield", data={"price": "1500000", "rent": "120000", "service": "20000", "vacancy": "0"}).status_code == 200


def test_validation_failures_do_not_lock_out_the_shared_limiter_or_capture():
    c = client(capture_origins=["https://a.example"])
    for _ in range(8):
        assert c.post("/p/lead", data={"tool": "costs", "price": "2000000", "name": "X", "phone": "0501234567", "consent": ""}).status_code == 422
    ok = c.post("/p/lead", data={"tool": "costs", "price": "2000000", "name": "X", "phone": "0501234567", "consent": "1"})
    assert ok.status_code == 200 and len(c.get("/leads").json()) == 1
    cap = c.post("/capture", json={"phone": "0509999999", "consent": True, "message": "buy a 2BR in Dubai Marina"}, headers={"Origin": "https://a.example"})
    assert cap.status_code == 200


def test_concierge_page_loads_do_not_consume_a_session_or_cap():
    c = client()
    for _ in range(10):
        assert c.get("/p/concierge").status_code == 200
    assert c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 0
    r, f = start(c)
    assert f["token"] == "" and f["step"] == "intent"
    r, f = step(c, f, "buy")
    assert f["token"] and c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 1
    assert dict(c.app.state.db.q("SELECT key,value FROM stats WHERE key='concierge_start'")) == {"concierge_start": 1}


def test_concierge_starts_are_rate_limited_per_ip():
    c = client()
    codes = [c.post("/p/concierge", data={"lang": "en", "token": "", "step": "intent", "answer": "buy"}).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]


def test_timeline_reaches_the_lead():
    c = client()
    r, f = start(c)
    for a in ("buy", "Dubai Marina", "2M", "this month", "yes"):
        r, f = step(c, f, a)
    c.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "S", "phone": "0501234567"})
    assert leads(c)[0]["timeline_days"] == 30
    c2 = client()
    r, f = start(c2)
    for a in ("buy", "Dubai Marina", "2M", "just exploring", "yes"):
        r, f = step(c2, f, a)
    c2.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "S", "phone": "0501234567"})
    assert leads(c2)[0]["timeline_days"] is not None and leads(c2)[0]["timeline_days"] >= 90


def test_consent_evidence_is_the_real_question_and_the_real_answer():
    c = client()
    r, f = start(c)
    for a in ("buy", "Dubai Marina", "2M", "this month"):
        r, f = step(c, f, a)
    r, f = step(c, f, "ok sure, yes")                                              # parsed as a yes, evidence keeps the visitor's own words
    r = c.post("/p/concierge", data={"lang": "ar", "token": f["token"], "step": "contact", "name": "S", "phone": "0501234567"})   # a tampered lang
    text = leads(c)[0]["consent_evidence"]["text"]
    assert "May an agent contact you" in text and text.endswith("ok sure, yes") and "نعم" not in text and "→" in text


def test_stop_works_even_from_a_stale_step():
    c = client()
    r, f = start(c)
    r, f2 = step(c, f, "buy")
    r = c.post("/p/concierge", data={"lang": "en", "token": f2["token"], "step": "intent", "answer": "STOP"})             # stale step name
    assert "We have not saved anything" in r.text and c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 0


def test_body_limits_by_content_length_and_stream():
    c = client()
    assert c.post("/p/costs", content=b"price=" + b"1" * 20000, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413

    def chunks():
        for _ in range(40):
            yield b"price=1&" * 100
    assert c.post("/p/costs", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_non_form_bodies_are_harmless():
    c = client()
    assert c.post("/p/lead", json={"tool": "costs"}).status_code == 404
    assert c.post("/p/concierge", json={"token": "x"}).status_code == 200
    assert c.post("/p/costs", content=b"--x\r\nContent-Disposition: form-data; name=price\r\n\r\n1\r\n--x--", headers={"content-type": "multipart/form-data; boundary=x"}).status_code == 422


@pytest.mark.parametrize("origins", [["https://ok.example"], ["https://ok.example", "https://bad.example\t*"], ["https://ok.example", "https://bäd.example"],
                                     ["https://ok.example", "*"], ["https://ok.example", "https://x.example;script-src *"], ["https://ok.example\n"]])
def test_csp_only_accepts_clean_origins(origins):
    out = csp(origins)
    out.encode("latin-1")                                                           # always a valid header value
    assert "*" not in out and ";" not in out.split("frame-ancestors")[1] and "bad" not in out and "script-src *" not in out and "\t" not in out and "\n" not in out


def test_asset_links_follow_a_path_prefix_in_public_url():
    c = client(public_url="https://x.example/leads")
    html = c.get("/p").text
    assert 'href="/leads/site-static/pub.css"' in html
    assert 'href="/site-static/pub.css"' in client().get("/p").text


def test_names_are_cleaned_of_control_characters():
    c = client()
    c.post("/p/lead", data={"tool": "costs", "price": "2000000", "name": "Sa\x00ra\r\nBuyer", "phone": "0501234567", "consent": "1"})
    assert "\x00" not in (leads(c)[0]["display_name"] or "") and "\n" not in leads(c)[0]["display_name"]


@pytest.mark.parametrize("raw", ["1,2,3", "１２３", "1 2 3 4", "1,23,456", "1,,000"])
def test_loose_numbers_are_rejected(raw):
    assert forms.parse_number(raw) is None


@pytest.mark.parametrize("raw,val", [("1,000,000", 1_000_000.0), ("25000", 25000.0), ("1 000 000", 1_000_000.0), ("٢٥٠٠٠", 25000.0), ("2.5", 2.5), ("0.5", 0.5)])
def test_normal_numbers_still_parse(raw, val):
    assert forms.parse_number(raw) == val


@pytest.mark.parametrize("phrase", ["earlier this month I sold my flat", "last month", "since this month began"])
def test_this_month_is_not_matched_in_the_past(phrase):
    assert parse_timeline(normalize(phrase)) is None
    assert parse_timeline(normalize("earlier this month I sold my flat")) is None
    assert parse_timeline(normalize("this month")) == 30


def test_affordability_lead_budget_falls_back_when_ltv_cap_is_zero():
    from leadengine.config import Rates
    from leadengine.magnets import forms
    inputs = {"income": 30000.0, "debts": 0.0, "down": 0.0, "years": 25, "rate": 4.5}
    result, err = forms.compute("affordability", inputs, Rates())
    assert err is None and result["ltv_capped_price"] == 0 and result["max_price"] > 0
    _, extra = forms.summary("affordability", inputs, result)
    assert extra["budget"] == f"AED {result['max_price']:.0f}"                 # never "AED 0"
