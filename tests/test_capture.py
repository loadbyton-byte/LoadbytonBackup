import json

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.capture import CONSENT_TEXT, RateLimiter, handle_capture, widget_js
from leadengine.config import Settings
from leadengine.db import DB

OK = {"name": "Aisha K", "phone": "0501234567", "email": "a@x.com", "looking_to": "buy", "area": "Dubai Marina",
      "budget": "AED 1.8M", "message": "2BR please", "consent": True}
ORIGIN = "https://agency.ae"


@pytest.fixture()
def env(pipe, settings):
    settings.capture_origins = [ORIGIN]
    return pipe, settings, RateLimiter(3, 600)


def cap(env, payload=OK, origin=ORIGIN, ip="1.1.1.1"):
    pipe, settings, lim = env
    return handle_capture(pipe, settings, payload, origin, ip, lim)


def n_leads(env):
    return env[0].db.one("SELECT COUNT(*) n FROM leads")["n"]


def test_rejects_bad_origin(env):
    assert cap(env, origin="https://evil.example")[0] == 403 and n_leads(env) == 0


def test_same_host_allowed_when_no_allowlist(pipe, settings):
    settings.capture_origins = []
    lim = RateLimiter(5, 60)
    assert handle_capture(pipe, settings, OK, "https://leads.agency.ae", "1.1.1.1", lim, host="leads.agency.ae")[0] == 200
    assert handle_capture(pipe, settings, OK, "https://evil.example", "1.1.1.1", lim, host="leads.agency.ae")[0] == 403


def test_honeypot_silently_ok_not_stored(env):
    assert cap(env, {**OK, "website": "http://spam"}) == (200, {"ok": True}) and n_leads(env) == 0


def test_requires_consent_and_contact(env):
    assert cap(env, {**OK, "consent": False}, ip="9.0.0.1")[0] == 400
    assert cap(env, {k: v for k, v in OK.items() if k != "consent"}, ip="9.0.0.2")[0] == 400
    assert cap(env, {**OK, "phone": "", "email": ""}, ip="9.0.0.3")[0] == 400
    assert cap(env, {**OK, "phone": "12", "email": "nope"}, ip="9.0.0.4")[0] == 400
    assert n_leads(env) == 0


def test_rate_limited_after_n(env):
    codes = [cap(env, {**OK, "phone": f"05012345{i:02d}"})[0] for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert cap(env, ip="2.2.2.2")[0] == 200            # other IPs unaffected


def test_oversize_rejected(env):
    assert cap(env, {**OK, "message": "x" * 20000})[0] == 413 and n_leads(env) == 0


def test_happy_path_creates_opted_in_lead_with_evidence(env):
    status, body = cap(env)
    assert (status, body) == (200, {"ok": True})
    lead = env[0].db.one("SELECT * FROM leads")
    assert lead["consent_status"] == "opted_in" and lead["phone"] == "+971501234567" and lead["primary_area"] == "Dubai Marina"
    assert json.loads(lead["consent_evidence"])["text"] == CONSENT_TEXT


def test_fields_are_capped_and_control_chars_stripped(env):
    cap(env, {**OK, "name": "A\x00" + "n" * 500})
    name = env[0].db.one("SELECT display_name FROM leads")["display_name"]
    assert len(name) <= 100 and "\x00" not in name


def test_suppressed_person_gets_same_ok_response(env):
    env[0].opt_out(phone="0501234567")
    assert cap(env) == (200, {"ok": True}) and n_leads(env) == 0


def test_widget_js_contents():
    js = widget_js('https://api.example/"x')
    assert "/capture" in js and "type:'checkbox'" in js and CONSENT_TEXT in js and "name:'website'" in js
    assert 'https://api.example/\\"x' in js                           # base url is JSON-escaped, not injectable


def test_http_layer_cors_and_widget_route():
    s = Settings(db_path=":memory:", api_key="", capture_origins=[ORIGIN])
    c = TestClient(create_app(s, DB(":memory:")))
    r = c.post("/capture", json=OK, headers={"origin": ORIGIN})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN
    bad = c.post("/capture", json=OK, headers={"origin": "https://evil.example"})
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers
    pre = c.options("/capture", headers={"origin": ORIGIN, "access-control-request-method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == ORIGIN
    assert c.post("/capture", content=b"x" * 20000, headers={"origin": ORIGIN}).status_code == 413
    w = c.get("/widget.js")
    assert w.status_code == 200 and "javascript" in w.headers["content-type"]
