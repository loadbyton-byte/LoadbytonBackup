import re

import pytest
from fastapi.testclient import TestClient

from leadengine.config import Settings
from leadengine.platform.app import create_platform_app
from leadengine.platform.mailer import ConsoleMailer
from leadengine.platform.security import generate_key, totp_code

from .conftest import PW

SIG = [{"platform": "reddit", "external_id": "x1", "text": "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, this week", "author_handle": "person_one"}]


def make_app(tmp_path, **kw):
    s = Settings(db_path=":memory:", control_db=":memory:", data_dir=str(tmp_path), encryption_key=generate_key(), base_url="http://testserver",
                 dev_insecure_cookies=kw.pop("dev_insecure_cookies", True), **kw)
    mailer = ConsoleMailer()
    return create_platform_app(s, mailer, inline_mail=True), mailer


@pytest.fixture()
def env(tmp_path):
    app, mailer = make_app(tmp_path)
    return app, mailer


def link_token(mailer, kind):
    for m in reversed(mailer.outbox):
        hit = re.search(rf"/{kind}\?token=([A-Za-z0-9_-]+)", m["text"])
        if hit:
            return hit.group(1)
    raise AssertionError(f"no {kind} mail")


def register(c, mailer, email="owner@a.ae", agency="Acme Realty", verify=True):
    r = c.post("/auth/register", json={"email": email, "password": PW, "name": "Aisha", "agency_name": agency, "lang": "en"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    if verify:
        assert c.post("/auth/verify", json={"token": link_token(mailer, "verify")}).status_code == 200


def login(c, email="owner@a.ae", pw=PW):
    r = c.post("/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    csrf = c.get("/auth/me").json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def signed_in(app, mailer, email="owner@a.ae", agency="Acme Realty"):
    c = TestClient(app)
    register(c, mailer, email, agency)
    return c, login(c, email)


def test_register_login_me_logout_roundtrip(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    me = c.get("/auth/me").json()
    assert me["user"]["email"] == "owner@a.ae" and me["role"] == "owner" and me["user"]["email_verified"] is True
    assert me["workspace"]["name"] == "Acme Realty" and me["workspace"]["public_key"].startswith("pk_") and me["mfa_enabled"] is False
    assert c.post("/auth/logout", headers=h).status_code == 200
    assert c.get("/auth/me").status_code == 401


def test_cookie_flags(tmp_path):
    app, mailer = make_app(tmp_path, dev_insecure_cookies=False)
    c = TestClient(app)
    register(c, mailer)
    r = c.post("/auth/login", json={"email": "owner@a.ae", "password": PW})
    cookies = {h.split("=")[0]: h for h in r.headers.get_list("set-cookie")}
    s, csrf = cookies["dbl_session"].lower(), cookies["dbl_csrf"].lower()
    assert "httponly" in s and "secure" in s and "samesite=lax" in s and "path=/" in s
    assert "httponly" not in csrf and "secure" in csrf and "samesite=lax" in csrf


def test_login_failure_shapes_identical(env):
    app, mailer = env
    c = TestClient(app)
    register(c, mailer)
    a = c.post("/auth/login", json={"email": "ghost@a.ae", "password": PW})
    b = c.post("/auth/login", json={"email": "owner@a.ae", "password": "wrong-password-1"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, a.json()) and "set-cookie" not in a.headers


def test_register_existing_email_looks_identical(env):
    app, mailer = env
    c = TestClient(app)
    first = c.post("/auth/register", json={"email": "o@a.ae", "password": PW, "name": "A", "agency_name": "X"})
    second = c.post("/auth/register", json={"email": "O@a.ae", "password": PW, "name": "A", "agency_name": "X"})
    assert (first.status_code, first.json()) == (second.status_code, second.json())


def test_json_endpoints_require_json_content_type_and_bound_size(env):
    app, _ = env
    c = TestClient(app)
    assert c.post("/auth/login", content="email=a&password=b", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
    assert c.post("/auth/login", content='{"email":"' + "a" * 40000 + '"}', headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/auth/login", content="not json", headers={"content-type": "application/json"}).status_code == 400


def test_csrf_required_on_cookie_post(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    other, oh = signed_in(app, mailer, "two@b.ae", "Beta")
    assert c.post("/agents", json={"name": "A"}).status_code == 403
    assert c.post("/agents", json={"name": "A"}, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert c.post("/agents", json={"name": "A"}, headers=oh).status_code == 403            # another user's token
    assert c.post("/agents", json={"name": "A"}, headers=h).status_code == 200
    assert c.get("/agents").status_code == 200                                              # safe methods need no token


def test_api_key_requests_skip_csrf_and_get_manager_rights(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    key = c.post("/auth/api-key", headers=h).json()["api_key"]
    machine = TestClient(app)
    r = machine.post("/ingest/signals", json=SIG, headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["created"] == 1
    assert machine.delete("/leads/1", headers={"X-API-Key": key}).status_code == 403        # manager rights: no owner-only routes
    assert machine.get("/leads", headers={"X-API-Key": "dbl_wrong"}).status_code == 401
    assert c.get("/leads").json()[0]["id"] == 1                                              # same workspace as the owner session


def test_api_key_header_is_authoritative_over_a_session(env):
    app, mailer = env
    a, ah = signed_in(app, mailer, "a@a.ae", "A")
    b, bh = signed_in(app, mailer, "b@b.ae", "B")
    key_a = a.post("/auth/api-key", headers=ah).json()["api_key"]
    a.post("/ingest/signals", json=SIG, headers={"X-API-Key": key_a})
    assert [x["id"] for x in a.get("/leads").json()] == [1] and b.get("/leads").json() == []
    mixed = b.get("/leads", headers={"X-API-Key": key_a})                                    # B's session + A's key => A only
    assert mixed.status_code == 200 and len(mixed.json()) == 1
    assert b.get("/leads", headers={"X-API-Key": "dbl_bogus"}).status_code == 401           # a bad key never falls back to the session


def test_unauthenticated_api_401_and_unknown_path_404(env):
    app, _ = env
    c = TestClient(app)
    for p in ("/leads", "/agents", "/roi", "/inventory", "/overview"):
        assert c.get(p).status_code == 401
    assert c.get("/definitely-not-a-route").status_code == 404


def test_forgot_and_reset_over_http(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    mailer.outbox.clear()
    assert c.post("/auth/forgot", json={"email": "ghost@a.ae"}).json() == {"ok": True} and not mailer.outbox
    assert c.post("/auth/forgot", json={"email": "owner@a.ae"}).json() == {"ok": True}
    tok = link_token(mailer, "reset")
    assert c.post("/auth/reset", json={"token": tok, "password": "a brand new passphrase"}).status_code == 200
    assert c.get("/auth/me").status_code == 401                                                # sessions revoked
    assert c.post("/auth/reset", json={"token": tok, "password": "another passphrase!!"}).status_code == 400
    assert c.post("/auth/reset", json={"token": "x", "password": "short"}).status_code == 400
    assert login(c, "owner@a.ae", "a brand new passphrase")


def test_session_cookie_rotates_on_login(env):
    app, mailer = env
    c = TestClient(app)
    register(c, mailer)
    r = c.post("/auth/login", json={"email": "owner@a.ae", "password": PW}, headers={"Cookie": "dbl_session=attacker-chosen-value"})
    assert r.status_code == 200 and c.cookies.get("dbl_session") not in (None, "attacker-chosen-value") and c.get("/auth/me").status_code == 200


def test_mfa_over_http(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    setup = c.post("/auth/mfa/begin", headers=h).json()
    from leadengine.platform import accounts as acc
    codes = c.post("/auth/mfa/confirm", json={"code": totp_code(setup["secret"])}, headers=h).json()["recovery_codes"]
    assert len(codes) == 10 and c.get("/auth/me").json()["mfa_enabled"] is True
    c2 = TestClient(app)
    r = c2.post("/auth/login", json={"email": "owner@a.ae", "password": PW}).json()
    assert r["status"] == "mfa_required" and "set-cookie" not in c2.post("/auth/login", json={"email": "owner@a.ae", "password": PW}).headers
    assert c2.post("/auth/login/mfa", json={"mfa_token": r["mfa_token"], "code": "000000"}).status_code == 401
    ok = c2.post("/auth/login/mfa", json={"mfa_token": r["mfa_token"], "code": codes[0]})
    assert ok.status_code == 200 and c2.get("/auth/me").status_code == 200 and acc


def test_invite_flow_over_http_and_role_limits(env):
    app, mailer = env
    owner, oh = signed_in(app, mailer)
    assert owner.post("/auth/invite", json={"email": "mgr@a.ae", "role": "manager"}, headers=oh).json() == {"ok": True}
    tok = link_token(mailer, "invite")
    m = TestClient(app)
    assert m.post("/auth/invite/accept", json={"token": tok, "password": PW, "name": "Mona"}).status_code == 200
    mh = login(m, "mgr@a.ae")
    assert m.get("/auth/me").json()["role"] == "manager" and m.get("/agents").status_code == 200
    assert m.post("/auth/invite", json={"email": "x@a.ae", "role": "agent"}, headers=mh).status_code == 403
    assert m.post("/auth/api-key", headers=mh).status_code == 403
    assert m.delete("/leads/1", headers=mh).status_code == 403


def test_signup_mode_invite_blocks_open_registration(tmp_path):
    app, _ = make_app(tmp_path, signup_mode="invite")
    assert TestClient(app).post("/auth/register", json={"email": "o@a.ae", "password": PW, "name": "A", "agency_name": "X"}).status_code == 403


@pytest.mark.parametrize("path", ["/leads/../agents", "//leads", "/leads/%2e%2e/agents", "/leads%2Fx", "/leads/./1", "/agents/%2e%2e/audit"])
def test_path_tricks_never_bypass_policy(env, path):
    app, mailer = env
    owner, oh = signed_in(app, mailer)
    tok_resp = owner.post("/auth/invite", json={"email": "ag@a.ae", "role": "agent"}, headers=oh)
    assert tok_resp.status_code == 200
    agent = TestClient(app)
    agent.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": PW, "name": "Omar"})
    login(agent, "ag@a.ae")
    for method in ("GET", "POST", "DELETE"):
        r = agent.request(method, path, headers={"X-CSRF-Token": agent.get("/auth/me").json()["csrf_token"]})
        assert r.status_code in (400, 403, 404, 405), (method, path, r.status_code)


def test_head_and_options_follow_policy(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    assert c.head("/leads").status_code == 200
    assert c.options("/leads").status_code in (403, 405)


def test_unauthenticated_requests_cannot_reach_tenant_only_routes(env):
    app, _ = env
    c = TestClient(app)
    for p in ("/capture", "/widget.js", "/optout", "/webhooks/whatsapp", "/health", "/docs", "/openapi.json"):
        assert c.get(p).status_code in (404, 405), p


def test_forwarded_requests_do_not_carry_credentials_to_the_tenant_app(env):
    app, mailer = env
    c, h = signed_in(app, mailer)
    r = c.get("/leads")
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"


def test_control_db_without_encryption_key_refuses_to_start(tmp_path):
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        create_platform_app(Settings(db_path=":memory:", control_db=":memory:", data_dir=str(tmp_path), encryption_key=""))
    with pytest.raises(ValueError, match="CONTROL_DB"):
        create_platform_app(Settings(db_path=":memory:", control_db="", encryption_key=generate_key()))


def test_healthz(env):
    assert TestClient(env[0]).get("/healthz").json() == {"ok": True}
