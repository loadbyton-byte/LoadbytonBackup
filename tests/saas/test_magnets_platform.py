import re

from fastapi.testclient import TestClient

from .test_app_auth import SIG, link_token, login, make_app, signed_in

AFF = {"income": "30000", "debts": "5000", "down": "200000", "years": "25", "rate": "4.5"}


def key_of(c):
    return c.get("/auth/me").json()["workspace"]["public_key"]


def lead_form(tool="affordability", **kw):
    return {"tool": tool, **AFF, "name": "Sara", "phone": "0501234567", "consent": "1", **kw}


def test_pages_are_public_per_workspace_and_private_paths_stay_private(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    key = key_of(c)
    anon = TestClient(app)
    for path in ("/p", "/p/costs", "/p/affordability", "/p/concierge"):
        assert anon.get(f"/t/{key}{path}").status_code == 200, path
    unknown = [anon.get(f"/t/{key}/p/unknown"), anon.get(f"/t/{key}/p/%2e%2e/leads")]
    assert unknown[0].status_code == 404 and unknown[1].status_code == 404
    bodies = {anon.get("/t/pk_nope/p").text, anon.get(f"/t/{key}x/p").text, anon.get(f"/t/{key}/leads").text, anon.get(f"/t/{key}/magnets/stats").text,
              anon.get(f"/t/{key}/p//x").text, anon.get(f"/t/{key}/p/../leads").text}
    assert len(bodies) == 1 and all(anon.get(u).status_code == 404 for u in (f"/t/{key}/leads", f"/t/{key}/magnets/stats", "/t/pk_nope/p"))
    assert anon.get("/leads").status_code == 401 and anon.get("/magnets/stats").status_code == 401 and anon.get("/p").status_code == 404


def test_form_actions_and_links_stay_under_the_workspace_path(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    key = key_of(c)
    html = TestClient(app).get(f"/t/{key}/p/costs").text
    assert f'action="/t/{key}/p/costs"' in html and f'href="http://testserver/t/{key}/p/costs?lang=en"' in html


def test_lead_through_the_public_path_lands_only_in_that_workspace(tmp_path):
    app, mailer = make_app(tmp_path)
    a, ah = signed_in(app, mailer, "a@a.ae", "Alpha")
    b, bh = signed_in(app, mailer, "b@b.ae", "Beta")
    anon = TestClient(app)
    assert anon.post(f"/t/{key_of(a)}/p/lead", data=lead_form()).status_code == 200
    assert len(a.get("/leads").json()) == 1 and b.get("/leads").json() == []
    r = anon.get(f"/t/{key_of(a)}/p/concierge")
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    other = anon.post(f"/t/{key_of(b)}/p/concierge", data={"lang": "en", "token": token, "step": "intent", "answer": "buy"})
    assert "expired" in other.text                                                  # B's database has never seen A's session


def test_frame_ancestors_follow_capture_origins_and_no_xfo_on_pages(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    key, anon = key_of(c), TestClient(app)
    r = anon.get(f"/t/{key}/p")
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"] and "https://www.agency.ae" not in r.headers["content-security-policy"]
    assert "x-frame-options" not in r.headers
    c.put("/auth/settings", json={"capture_origins": ["https://www.agency.ae"]}, headers=h)
    r = anon.get(f"/t/{key}/p")
    assert "frame-ancestors 'self' https://www.agency.ae" in r.headers["content-security-policy"] and "x-frame-options" not in r.headers
    assert anon.get("/login").headers["x-frame-options"] == "DENY" and c.get("/auth/me").headers["x-frame-options"] == "DENY"
    assert anon.get(f"/t/{key}/widget.js").headers["x-frame-options"] == "DENY"


def test_stats_endpoint_roles_and_counts(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    key, anon = key_of(owner), TestClient(app)
    anon.get(f"/t/{key}/p/affordability")
    anon.post(f"/t/{key}/p/affordability", data=AFF)
    anon.post(f"/t/{key}/p/lead", data=lead_form())
    anon.get(f"/t/{key}/p/concierge")
    s = owner.get("/magnets/stats").json()
    assert s["tools"]["affordability"] == {"views": 1, "submits": 1, "leads": 1} and s["concierge"] == {"started": 1, "completed": 0, "declined": 0}
    assert s["tools"]["costs"] == {"views": 0, "submits": 0, "leads": 0}
    api_key = owner.post("/auth/api-key", headers=oh).json()["api_key"]
    assert TestClient(app).get("/magnets/stats", headers={"X-API-Key": api_key}).status_code == 200
    owner.post("/auth/invite", json={"email": "ag@a.ae", "role": "agent"}, headers=oh)
    ag = TestClient(app)
    ag.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "Ag"})
    login(ag, "ag@a.ae")
    assert ag.get("/magnets/stats").status_code == 403
    other, _ = signed_in(app, mailer, "o@o.ae", "Other")
    assert other.get("/magnets/stats").json()["tools"]["affordability"]["views"] == 0


def test_settings_lists_public_pages_and_iframe_snippets(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    key = key_of(owner)
    s = owner.get("/auth/settings").json()
    assert [p["tool"] for p in s["public_pages"]] == ["affordability", "costs", "yield", "mortgage", "concierge"]
    for p in s["public_pages"]:
        assert p["url"].endswith(f"/t/{key}/p/{p['tool']}") and f'src="{p["url"]}"' in p["iframe"] and 'title="' in p["iframe"]
    owner.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=oh)
    m = TestClient(app)
    m.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "M"})
    login(m, "m@a.ae")
    assert m.get("/auth/settings").status_code == 403
