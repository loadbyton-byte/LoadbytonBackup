"""Two workspaces, every tenant route, the other workspace's credentials: nothing may cross."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from leadengine.platform.rbac import allowed

from .test_app_auth import SIG, link_token, login, make_app, register, signed_in

MARK_A, MARK_B = "ZZ-ALPHA-MARK", "QQ-BETA-MARK"
OPEN_ROUTES = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/capture", "/widget.js", "/optout"}


@pytest.fixture()
def two(tmp_path):
    app, mailer = make_app(tmp_path)
    a, ah = signed_in(app, mailer, "owner@alpha.ae", "Alpha Realty")
    b, bh = signed_in(app, mailer, "owner@beta.ae", "Beta Realty")
    for c, h, mark in ((a, ah, MARK_A), (b, bh, MARK_B)):
        sig = [{**SIG[0], "external_id": f"s-{mark}", "author_handle": f"person_{mark.lower()}", "author_name": mark, "phone": "0501234567" if mark == MARK_A else "0509876543"}]
        assert c.post("/ingest/signals", json=sig, headers=h).status_code == 200
        c.post("/agents", json={"name": f"Agent {mark}"}, headers=h)
        c.post("/spend", json={"source": "reddit", "period": "2026-10", "amount_aed": 111 if mark == MARK_A else 222}, headers=h)
        c.post("/inventory", content=f"name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\n{mark},Dev,Dubai Marina,Dubai,1.2M,2M,2,2030,60/40\n",
               headers={**h, "content-type": "text/csv"})
        c.post("/leads/1/viewings", json={"start": "2030-03-10T10:00:00+04:00", "location": mark}, headers=h)
    return app, (a, ah), (b, bh)


def test_every_tenant_route_is_scoped(two):
    app, (a, ah), (b, bh) = two
    inner = app.state.tenants.app_for(1)
    seen = 0
    for r in inner.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if not methods or path in OPEN_ROUTES or path.startswith("/webhooks") or path.startswith("/p/") or path == "/p":
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            url = (path.replace("{lead_id}", "1").replace("{agent_id}", "1").replace("{name}", "reddit").replace("{country}", "GB").replace("{platform}", "tiktok"))
            if not allowed("owner", method, url):
                continue
            seen += 1
            body = {"json": {}} if method in ("POST", "PATCH") else {}
            if path in ("/ingest/csv", "/inventory", "/ingest/portal-email"):
                body = {"content": "x"}
            resp = b.request(method, url, headers=bh, **body)
            text = resp.text
            assert MARK_A not in text and "0501234567" not in text and "owner@alpha.ae" not in text, f"{method} {url} leaked workspace A data"
    assert seen >= 30                                                    # the enumeration really ran over the API surface
    assert a.get("/leads").json()[0]["display_name"] == MARK_A and len(a.get("/leads").json()) == 1       # A untouched by B's calls


def test_workspace_data_is_disjoint_across_read_endpoints(two):
    _, (a, _), (b, _) = two
    for path, marker_a, marker_b in (("/leads", MARK_A, MARK_B), ("/agents", MARK_A, MARK_B), ("/inventory", MARK_A, MARK_B),
                                      ("/viewings", MARK_A, MARK_B), ("/roi", "111", "222")):
        ta, tb = a.get(path).text, b.get(path).text
        assert marker_a in ta and marker_b not in ta, path
        assert marker_b in tb and marker_a not in tb, path


def test_guessed_ids_return_404_not_other_tenant_data(two):
    _, (a, ah), (b, bh) = two
    b.post("/ingest/signals", json=[{**SIG[0], "external_id": "second", "author_handle": "person_second_b"}], headers=bh)
    assert a.get("/leads/2").status_code == 404                           # A has only lead 1; B's lead 2 is invisible
    assert b.get("/leads/2").status_code == 200
    for lid in (3, 99, 10**9):
        assert a.get(f"/leads/{lid}").status_code == 404
    assert a.delete("/leads/2", headers=ah).status_code == 404 and b.get("/leads/2").status_code == 200


def test_public_key_of_other_tenant_cannot_write_here(two):
    app, (a, _), (b, _) = two
    key_a = a.get("/auth/me").json()["workspace"]["public_key"]
    key_b = b.get("/auth/me").json()["workspace"]["public_key"]
    anon = TestClient(app)
    r = anon.post(f"/t/{key_b}/capture", json={"name": "Visitor", "phone": "0505555555", "consent": True, "message": MARK_B})
    assert r.status_code == 200
    assert len(b.get("/leads").json()) == 2 and len(a.get("/leads").json()) == 1
    assert key_a != key_b


def test_wrong_or_unknown_key_identical_404(two):
    app, (a, _), _ = two
    anon = TestClient(app)
    key_a = a.get("/auth/me").json()["workspace"]["public_key"]
    bad = [anon.post("/t/pk_doesnotexist0000000000/capture", json={}), anon.post("/t/nope/capture", json={}), anon.post(f"/t/{key_a}x/capture", json={}),
           anon.get(f"/t/{key_a}/leads"), anon.get(f"/t/{key_a}/agents"), anon.get(f"/t/{key_a}/../leads"), anon.post(f"/t/{key_a}/maintenance/purge")]
    assert all(r.status_code == 404 for r in bad)
    assert len({r.text for r in bad}) == 1


def test_public_routes_expose_only_the_public_surface(two):
    app, (a, _), _ = two
    key = a.get("/auth/me").json()["workspace"]["public_key"]
    anon = TestClient(app)
    assert anon.get(f"/t/{key}/widget.js").status_code == 200 and f"/t/{key}" in anon.get(f"/t/{key}/widget.js").text
    for p in ("/leads", "/agents", "/health", "/docs", "/openapi.json", "/demo/seed", "/stats", "/ingest/signals"):
        assert anon.request("GET" if p != "/ingest/signals" else "POST", f"/t/{key}{p}").status_code == 404, p


def test_session_of_one_tenant_cannot_write_to_other_with_ids_in_body(two):
    _, (a, ah), (b, bh) = two
    assert a.post("/leads/1/assign", headers=ah).status_code == 200
    assert b.post("/leads/1/status", json={"status": "won"}, headers=bh).status_code == 200            # B's own lead 1
    assert a.get("/leads/1").json()["status"] != "won"                                                   # A's lead 1 unaffected


def test_webhook_signature_is_per_tenant(two):
    app, (a, ah), (b, bh) = two
    app.state.tenants.set_setting(1, "meta_app_secret", "secret-of-alpha")
    app.state.tenants.set_setting(2, "meta_app_secret", "secret-of-beta")
    app.state.tenants.evict(1)
    app.state.tenants.evict(2)
    key_a = a.get("/auth/me").json()["workspace"]["public_key"]
    key_b = b.get("/auth/me").json()["workspace"]["public_key"]
    body = json.dumps({"entry": []}).encode()
    sig_a = "sha256=" + hmac.new(b"secret-of-alpha", body, hashlib.sha256).hexdigest()
    anon = TestClient(app)
    assert anon.post(f"/t/{key_a}/webhooks/meta", content=body, headers={"x-hub-signature-256": sig_a}).status_code == 200
    assert anon.post(f"/t/{key_b}/webhooks/meta", content=body, headers={"x-hub-signature-256": sig_a}).status_code == 401


def test_deleted_workspace_is_gone_for_sessions_and_keys(two):
    app, (a, ah), (b, bh) = two
    key_a = a.post("/auth/api-key", headers=ah).json()["api_key"]
    app.state.tenants.delete_tenant(1)
    assert a.get("/leads").status_code == 401 and TestClient(app).get("/leads", headers={"X-API-Key": key_a}).status_code == 401
    assert b.get("/leads").status_code == 200 and MARK_A not in b.get("/leads").text


def test_session_for_tenant_a_never_resolves_to_b_after_membership_removed(two):
    app, (a, _), (b, _) = two
    with app.state.control.tx() as c:
        c.execute("DELETE FROM memberships WHERE tenant_id=1")
    assert a.get("/leads").status_code == 401 and b.get("/leads").status_code == 200
