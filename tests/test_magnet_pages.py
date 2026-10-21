import logging
import re

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.calculators import affordability, deal_costs, mortgage_payment, rental_yield
from leadengine.capture import CONSENT_TEXT
from leadengine.config import Rates, Settings
from leadengine.db import DB
from leadengine.platform import site

AFF = {"income": "30000", "debts": "5000", "down": "200000", "years": "25", "rate": "4.5"}
VALID = {"affordability": AFF, "costs": {"price": "2000000", "loan": "1500000"}, "yield": {"price": "1500000", "rent": "100000", "service": "12000", "vacancy": "5"},
         "mortgage": {"principal": "1500000", "rate": "4.5", "years": "25"}}
CONTACT = {"name": "Sara Buyer", "phone": "0501234567", "email": "", "consent": "1", "website": ""}


def client(**kw):
    return TestClient(create_app(Settings(db_path=":memory:", api_key="", **kw), DB(":memory:")))


def lead_post(c, tool="affordability", **over):
    return c.post("/p/lead", data={"tool": tool, **VALID[tool], **CONTACT, **over})


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("path", ["/p", "/p/affordability", "/p/costs", "/p/yield", "/p/mortgage"])
def test_every_page_renders_en_and_ar(path, lang):
    c = client()
    for r in (c.get(path, params={"lang": lang}), c.get(path, headers={"Accept-Language": lang})):
        assert r.status_code == 200
        assert f'dir="{"rtl" if lang == "ar" else "ltr"}"' in r.text and len(re.findall(r"<h1[ >]", r.text)) == 1
        csp = r.headers["content-security-policy"]
        assert "script-src 'none'" in csp and "frame-ancestors 'self'" in csp and r.headers["x-content-type-options"] == "nosniff"
        for fid in re.findall(r'<input id="(f-\w+)"', r.text):
            assert f'for="{fid}"' in r.text
    assert c.get("/p/unknown").status_code == 404


def test_calculators_show_results_without_contact_details():
    c = client()
    rates = Rates()
    expected = {"affordability": f"{affordability(30000, 5000, 200000, 25, 4.5, rates=rates)['max_price']:,.2f}",
                "costs": f"{deal_costs(2000000, rates, loan=1500000)['total_costs']:,.2f}",
                "yield": f"{rental_yield(1500000, 100000, service_charge=12000, vacancy_pct=5)['net_pct']:,.2f}%",
                "mortgage": f"{mortgage_payment(1500000, 4.5, 25):,.2f}"}
    for tool, data in VALID.items():
        r = c.post(f"/p/{tool}", data=data)
        assert r.status_code == 200 and expected[tool] in r.text, tool
        assert "Indicative only" in r.text and "no-store" in r.headers["cache-control"] and "noindex" in r.text
    assert c.get("/leads").json() == []


@pytest.mark.parametrize("bad", ["nan", "inf", "1e308", "-5", "0", "9" * 40, "", "abc", "1,2,3x", "٠٫٥٫٥", "1 000 000.5.5"])
def test_invalid_and_extreme_inputs_are_rejected_cleanly(bad):
    c = client()
    for lang, needles in (("en", ("plain numbers", "outside the allowed range")), ("ar", ("أرقاماً عادية", "خارج النطاق"))):
        r = c.post("/p/affordability", data={**AFF, "income": bad, "lang": lang})
        assert r.status_code == 422 and any(n in r.text for n in needles)
        assert "Traceback" not in r.text and not re.findall(r"<dd>[^<]*(nan|inf)[^<]*</dd>", r.text, re.I)
    assert c.post("/p/affordability", data={**AFF, "years": bad}).status_code == 422
    r = c.post("/p/affordability", content="income=30000&income=nan&years=25&rate=4.5", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422 and not re.findall(r"<dd>[^<]*(nan|inf)[^<]*</dd>", r.text, re.I)


def test_range_errors_are_translated_not_crashes():
    c = client()
    r = c.post("/p/affordability", data={**AFF, "years": "40"})
    assert r.status_code == 422 and "outside the allowed range" in r.text
    r = c.post("/p/costs", data={"price": "1000000", "loan": "2000000", "lang": "ar"})
    assert r.status_code == 422 and "خارج النطاق" in r.text


def test_reflected_values_are_escaped():
    c = client()
    xss = "<script>alert(1)</script>"
    for tool in ("affordability", "costs", "yield", "mortgage"):
        data = {f: xss for f in VALID[tool]} | {"lang": xss}
        r = c.post(f"/p/{tool}", data=data)
        assert "<script>alert" not in r.text and r.status_code == 422
    r = c.post("/p/lead", data={"tool": "affordability", **AFF, **CONTACT, "name": xss, "phone": xss, "email": xss, "consent": "", "lang": xss})
    assert "<script>alert" not in r.text
    assert "<script>alert" not in c.get("/p/affordability", params={"lang": xss}).text
    assert c.post("/p/lead", data={"tool": xss, **AFF}).status_code == 404 and "<script>alert" not in c.post("/p/lead", data={"tool": xss}).text


def test_lead_requires_consent_and_contact_and_honeypot():
    c = client()
    assert lead_post(c, consent="").status_code == 422 and c.get("/leads").json() == []
    assert lead_post(c, phone="", email="").status_code == 422 and c.get("/leads").json() == []
    assert lead_post(c, phone="12", email="nope").status_code == 422
    r = lead_post(c, website="http://spam.example")
    assert r.status_code == 200 and "Thank you" in r.text and c.get("/leads").json() == []


def test_lead_created_with_tag_prefill_and_consent_evidence():
    c = client()
    assert c.get("/p/affordability").status_code == 200
    c.post("/p/affordability", data=AFF)
    r = lead_post(c)
    assert r.status_code == 200 and "Thank you" in r.text
    leads = c.get("/leads").json()
    assert len(leads) == 1 and leads[0]["consent_status"] == "opted_in" and leads[0]["score"] > 0
    assert leads[0]["consent_evidence"]["text"] == CONSENT_TEXT
    full = c.get(f"/leads/{leads[0]['id']}").json()
    assert full["signals"][0]["platform"] == "web" and leads[0]["budget_max"] == 1_000_000.0
    stats = dict(c.app.state.db.q("SELECT key,value FROM stats WHERE key LIKE 'magnet_%'"))
    assert stats == {"magnet_affordability_view": 1, "magnet_affordability_submit": 1, "magnet_affordability_lead": 1}
    sig = c.app.state.db.one("SELECT campaign FROM signals")
    assert sig["campaign"] == "lead-magnet:affordability"


def test_arabic_lead_stores_the_arabic_consent_wording():
    c = client()
    assert lead_post(c, lang="ar").status_code == 200
    ev = c.get("/leads").json()[0]["consent_evidence"]["text"]
    assert ev == site.strings("ar")["pub.consent"] and ev != CONSENT_TEXT


def test_tampered_hidden_inputs_are_revalidated():
    c = client()
    for bad in ({"income": "nan"}, {"years": "0"}, {"rate": "99"}, {"income": ""}):
        assert lead_post(c, **bad).status_code == 422
    assert c.post("/p/lead", data={"tool": "nope", **AFF, **CONTACT}).status_code == 404
    assert c.get("/leads").json() == []


def test_duplicate_and_suppressed_look_identical():
    c = client()
    first, second = lead_post(c), lead_post(c)
    c.app.state.pipeline.opt_out(phone="0501234567")
    third = lead_post(c)
    assert first.status_code == second.status_code == third.status_code == 200 and first.text == second.text == third.text
    assert c.get("/leads").json() == []                                      # the opt-out erased the lead; the suppressed repeat created nothing


def test_rate_limit_and_body_limit():
    c = client()
    codes = [lead_post(c, website="x").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429 and "few minutes" in lead_post(c).text
    c2 = client()
    assert c2.post("/p/costs", content=b"price=" + b"1" * 9000, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_logs_contain_no_personal_data(caplog):
    c = client()
    with caplog.at_level(logging.DEBUG):
        lead_post(c, name="Zed Uniqueperson", phone="0509876543", email="zed.unique@example.com")
    for secret in ("Zed Uniqueperson", "0509876543", "zed.unique@example.com", "509876543"):
        assert secret not in caplog.text


def test_agent_details_shown_only_when_configured():
    plain = client().get("/p").text
    assert "BRN" not in plain
    shown = client(agent_name="Aisha Khan", brn="12345", trakheesi="T-99", agency_name="Acme Realty").get("/p").text
    assert "Aisha Khan" in shown and "12345" in shown and "T-99" in shown and "Acme Realty" in shown
