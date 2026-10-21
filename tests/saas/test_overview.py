import re

import pytest
from fastapi.testclient import TestClient

from .test_app_auth import SIG, link_token, login, make_app, signed_in

CSV = "name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\nMarina Edge,Acme,Dubai Marina,Dubai,1.2M,2.4M,2,2030,60/40\n"


def lead(i):
    return {**SIG[0], "external_id": f"o{i}", "author_handle": f"person_{i}_over"}


def test_overview_empty_workspace_all_zero_and_onboarding_false(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    o = c.get("/overview").json()
    assert (o["new_today"], o["hot"], o["warm"], o["overdue"]) == (0, 0, 0, 0) and o["within_sla_pct"] is None
    assert o["by_status"] == {s: 0 for s in ("new", "contacted", "qualified", "viewing", "won", "lost")}
    assert o["onboarding"] == {"agents": False, "inventory": False, "widget_origin": False, "whatsapp": False}
    assert len(o["trend"]) == 14 and all(t["leads"] == 0 for t in o["trend"])


def test_overview_counts_and_trend_length_14(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    c.post("/ingest/signals", json=[lead(1), lead(2), lead(3)], headers=h)
    o = c.get("/overview").json()
    assert o["new_today"] == 3 and o["hot"] + o["warm"] >= 1 and o["by_status"]["new"] == 3
    assert len(o["trend"]) == 14 and o["trend"][-1]["leads"] == 3 and sum(t["leads"] for t in o["trend"]) == 3
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", o["trend"][0]["date"]) and o["trend"][0]["date"] < o["trend"][-1]["date"]
    c.post("/leads/1/status", json={"status": "contacted"}, headers=h)
    o = c.get("/overview").json()
    assert o["by_status"]["contacted"] == 1 and o["by_status"]["new"] == 2


def test_onboarding_flags_flip_when_agent_inventory_origin_added(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    c.post("/agents", json={"name": "Ann"}, headers=h)
    assert c.get("/overview").json()["onboarding"]["agents"] is True
    c.post("/inventory", content=CSV, headers={**h, "content-type": "text/csv"})
    assert c.get("/overview").json()["onboarding"]["inventory"] is True
    r = c.put("/auth/settings", json={"capture_origins": ["https://agency.example"], "whatsapp_phone_id": "123"}, headers=h)
    assert r.status_code == 200
    ob = c.get("/overview").json()["onboarding"]
    assert ob["widget_origin"] is True and ob["whatsapp"] is True          # settings change reaches the running workspace app


def test_overview_is_manager_only(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    owner.post("/auth/invite", json={"email": "ag@a.ae", "role": "agent"}, headers=oh)
    a = TestClient(app)
    a.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "Ag"})
    login(a, "ag@a.ae")
    assert a.get("/overview").status_code == 403


# ---------------------------------------------------------------- settings + team API
def test_settings_roundtrip_hides_secrets_and_validates(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    g = c.get("/auth/settings").json()
    assert g["agency_name"] == "Acme Realty" and g["capture_origins"] == [] and g["secrets"]["meta_app_secret"] is False
    assert g["embed"]["widget_url"].endswith(f"/t/{c.get('/auth/me').json()['workspace']['public_key']}/widget.js")
    r = c.put("/auth/settings", json={"agent_name": "Aisha", "brn": "12345", "trakheesi": "T-1", "capture_origins": ["https://a.example", "http://b.example:8080"],
                                      "meta_app_secret": "s3cret-value"}, headers=h)
    assert r.status_code == 200
    g = c.get("/auth/settings").json()
    assert g["agent_name"] == "Aisha" and g["brn"] == "12345" and g["secrets"]["meta_app_secret"] is True
    assert "s3cret-value" not in c.get("/auth/settings").text
    assert c.put("/auth/settings", json={"capture_origins": ["javascript:alert(1)"]}, headers=h).status_code == 400
    assert c.put("/auth/settings", json={"capture_origins": ["https://x.example/path"]}, headers=h).status_code == 400
    assert c.put("/auth/settings", json={"agency_name": "x" * 500}, headers=h).status_code == 400
    assert c.put("/auth/settings", json={"api_key": "hack", "whatsapp_send_enabled": True}, headers=h).status_code == 400   # not settable here
    assert c.put("/auth/settings", json={"agent_name": "No CSRF"}).status_code == 403


def test_settings_owner_only_and_public_capture_uses_new_origin(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    owner.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=oh)
    m = TestClient(app)
    m.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "M"})
    mh = login(m, "m@a.ae")
    assert m.get("/auth/settings").status_code == 403 and m.put("/auth/settings", json={"agent_name": "x"}, headers=mh).status_code == 403
    key = owner.get("/auth/me").json()["workspace"]["public_key"]
    body = {"name": "Visitor", "phone": "0505555555", "consent": True, "message": "I want to buy a 2BR in Dubai Marina, budget AED 2M"}
    anon = TestClient(app)
    assert anon.post(f"/t/{key}/capture", json=body, headers={"Origin": "https://agency.example"}).status_code == 403
    owner.put("/auth/settings", json={"capture_origins": ["https://agency.example"]}, headers=oh)
    assert anon.post(f"/t/{key}/capture", json=body, headers={"Origin": "https://agency.example"}).status_code == 200


def test_team_lists_members_and_pending_invites(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    t = c.get("/auth/team").json()
    assert [m["email"] for m in t["members"]] == ["owner@a.ae"] and t["members"][0]["role"] == "owner" and t["invites"] == []
    c.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=h)
    assert [i["email"] for i in c.get("/auth/team").json()["invites"]] == ["m@a.ae"]


# ---------------------------------------------------------------- /app shell
def test_app_shell_requires_session_and_ships_a_strict_csp(tmp_path):
    app, mailer = make_app(tmp_path)
    anon = TestClient(app)
    r = anon.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fapp"
    c, _ = signed_in(app, mailer)
    r = c.get("/app")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "no-store" in r.headers["cache-control"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "<script" in r.text and 'src="/app-assets/' in r.text and "onclick=" not in r.text
    for path in re.findall(r'(?:src|href)="(/app-assets/[^"]+)"', r.text):
        assert anon.get(path).status_code == 200, path
