"""Regression tests for the final whole-branch review."""
import re

import pytest
from fastapi.testclient import TestClient

from leadengine.platform import site

from .conftest import PW
from .test_app_auth import link_token, login, make_app, register, signed_in


def test_unverified_owner_cannot_store_secrets_or_origins(tmp_path):
    app, mailer = make_app(tmp_path)
    c = TestClient(app)
    register(c, mailer, verify=False)
    h = login(c)
    assert c.put("/auth/settings", json={"whatsapp_token": "SECRET123"}, headers=h).status_code == 403
    assert c.put("/auth/settings", json={"capture_origins": ["https://a.example"]}, headers=h).status_code == 403
    assert c.put("/auth/settings", json={"agent_name": "Fine"}, headers=h).status_code == 200      # harmless profile edits are allowed


def test_user_in_two_workspaces_can_switch(tmp_path):
    app, mailer = make_app(tmp_path)
    a, ah = signed_in(app, mailer, "a@a.ae", "AAA")
    b, bh = signed_in(app, mailer, "b@b.ae", "BBB")
    b.post("/auth/invite", json={"email": "a@a.ae", "role": "manager"}, headers=bh)
    r = a.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": PW, "name": "A"})
    assert r.status_code == 200
    login(a, "a@a.ae")
    me = a.get("/auth/me").json()
    assert me["workspace"]["name"] == "AAA" and {w["name"] for w in me["workspaces"]} == {"AAA", "BBB"}
    other = next(w for w in me["workspaces"] if w["name"] == "BBB")["id"]
    h = {"X-CSRF-Token": me["csrf_token"]}
    assert a.post("/auth/workspace", json={"tenant_id": other}, headers={}).status_code == 403          # CSRF
    assert a.post("/auth/workspace", json={"tenant_id": 999}, headers=h).status_code == 403             # not a member
    assert a.post("/auth/workspace", json={"tenant_id": other}, headers=h).status_code == 200
    me2 = a.get("/auth/me").json()
    assert me2["workspace"]["name"] == "BBB" and me2["role"] == "manager"
    assert a.post("/auth/workspace", json={"tenant_id": other}, headers={"X-CSRF-Token": me["csrf_token"]}).status_code == 403   # old token died with old session


def test_static_mount_never_serves_html_dashboards(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    for p in ("/static/app.html", "/static/index.html"):
        assert c.get(p).status_code == 404
    assert c.get("/static/brand/favicon.svg").status_code == 200 and c.get("/static/theme.css").status_code == 200


def test_agency_rename_applies_everywhere_and_control_chars_are_rejected(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    assert c.put("/auth/settings", json={"agency_name": "New Name Realty"}, headers=h).status_code == 200
    assert c.get("/auth/me").json()["workspace"]["name"] == "New Name Realty"
    assert c.put("/auth/settings", json={"agency_name": "Bad\r\nName"}, headers=h).status_code == 400
    r = TestClient(app).post("/auth/register", json={"email": "x@x.ae", "password": PW, "name": "X", "agency_name": "Evil\r\nBcc: a@b.c"})
    assert r.status_code == 400
    c.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=h)          # invite mail still works after a rename
    assert "New Name Realty" in mailer.outbox[-1]["subject"]


def test_language_toggle_keeps_the_token_query(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    html = c.get("/reset?token=abc123").text
    assert "/lang/ar?next=%2Freset%3Ftoken%3Dabc123" in html
    r = c.get("/lang/ar?next=%2Freset%3Ftoken%3Dabc123", follow_redirects=False)
    assert r.headers["location"] == "/reset?token=abc123"
    assert "abc123" not in c.get("/features").text


def test_canonical_matches_hreflang_and_sitemap_lists_language_urls(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    t = c.get("/features?lang=ar").text
    canon = re.search(r'rel="canonical" href="([^"]+)"', t).group(1)
    assert canon.endswith("/features?lang=ar") and f'hreflang="ar" href="{canon}"' in t
    x = c.get("/sitemap.xml").text
    assert "/features?lang=ar" in x and "/features?lang=en" in x and "SoftwareApplication" in t


def test_site_copy_does_not_promise_unavailable_platform_features():
    blob = " ".join(site.strings("en").values()).lower()
    for promise in ("calendar feed", "do not call file you provide", "until you enable it", "licensed provider you hold", "reddit through its official api", "you can export or delete it"):
        assert promise not in blob, promise


def test_html_dashboards_pages_use_host_prefixed_form_cookie_when_secure(tmp_path):
    app, _ = make_app(tmp_path, dev_insecure_cookies=False)
    r = TestClient(app).get("/login")
    assert "__Host-dbl_form=" in r.headers["set-cookie"]
