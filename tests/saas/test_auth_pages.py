import re

import pytest
from fastapi.testclient import TestClient

from leadengine.platform.security import totp_code

from .conftest import PW
from .test_app_auth import link_token, make_app, register


def csrf_of(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    m = re.search(r'name="_csrf" value="([0-9a-f]+)"', r.text)
    assert m, path
    return m.group(1)


def post(client, path, data, token=None, follow=False, **kw):
    if token is None:
        token = csrf_of(client, kw.pop("form_path", path))
    return client.post(path, data={"_csrf": token, **data}, follow_redirects=follow, **kw)


@pytest.fixture()
def env(tmp_path):
    app, mailer = make_app(tmp_path)
    return TestClient(app), mailer, app


def signup(c, mailer, email="owner@a.ae"):
    r = post(c, "/start", {"email": email, "password": PW, "name": "Aisha", "agency_name": "Acme Realty"})
    assert r.status_code == 303 and r.headers["location"] == "/start?sent=1"
    return r


def test_start_form_creates_account_and_redirects_to_verify_notice(env):
    c, mailer, _ = env
    signup(c, mailer)
    page = c.get("/start?sent=1")
    assert page.status_code == 200 and "Check" in page.text or "confirm" in page.text.lower()
    link = link_token(mailer, "verify")
    v = c.get(f"/verify?token={link}")
    assert v.status_code == 200 and "<form" in v.text                      # GET never consumes the token
    ok = post(c, "/verify", {"token": link}, form_path=f"/verify?token={link}")
    assert ok.status_code == 200 and "confirmed" in ok.text
    again = post(c, "/verify", {"token": link}, form_path=f"/verify?token={link}")
    assert again.status_code == 400


def test_login_form_sets_cookie_and_redirects_to_app(env):
    c, mailer, _ = env
    signup(c, mailer)
    r = post(c, "/login", {"email": "owner@a.ae", "password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookies = r.headers.get_list("set-cookie")
    assert any(x.startswith("dbl_session=") and "HttpOnly" in x for x in cookies) and any(x.startswith("dbl_csrf=") for x in cookies)
    assert c.get("/auth/me").status_code == 200


def test_forms_reject_missing_csrf(env):
    c, mailer, _ = env
    for path, data in (("/start", {"email": "a@a.ae", "password": PW, "name": "A", "agency_name": "X"}), ("/login", {"email": "a@a.ae", "password": PW}),
                       ("/forgot", {"email": "a@a.ae"}), ("/reset", {"token": "t", "password": PW}), ("/verify", {"token": "t"}), ("/invite", {"token": "t", "password": PW, "name": "n"})):
        c.get("/login")                                                    # have a form cookie
        assert c.post(path, data=data).status_code == 403, path               # no token
        assert c.post(path, data={**data, "_csrf": "0" * 40}).status_code == 403, path   # wrong token
        assert c.post(path, data=data, headers={"content-type": "application/json"}).status_code in (403, 415), path
    assert mailer.outbox == []
    # a token minted for another browser does not validate
    other = TestClient(c.app)
    tok = csrf_of(other, "/login")
    c2 = TestClient(c.app)
    c2.get("/login")
    assert c2.post("/login", data={"_csrf": tok, "email": "a@a.ae", "password": PW}).status_code == 403


def test_error_messages_are_uniform_and_translated(env):
    c, mailer, _ = env
    signup(c, mailer)
    for lang, needle in (("en", "Incorrect email or password."), ("ar", "غير صحيحة")):
        c.cookies.clear()
        bad_pw = post(c, "/login", {"email": "owner@a.ae", "password": "wrong-password-1"}, form_path=f"/login?lang={lang}", headers={"Accept-Language": lang})
        unknown = post(c, "/login", {"email": "ghost@a.ae", "password": "wrong-password-1"}, form_path=f"/login?lang={lang}", headers={"Accept-Language": lang})
        assert bad_pw.status_code == unknown.status_code == 401 and needle in bad_pw.text and needle in unknown.text
        strip = lambda t: re.sub(r'value="[^"]*"', "", t)
        assert strip(bad_pw.text) == strip(unknown.text) or needle in unknown.text
    weak = post(c, "/start", {"email": "n@a.ae", "password": "short", "name": "N", "agency_name": "N"})
    assert weak.status_code == 400 and "12" in weak.text and 'value="n@a.ae"' in weak.text
    assert 'value="short"' not in weak.text


def test_open_redirect_blocked(env):
    c, mailer, _ = env
    signup(c, mailer)
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        r = post(c, "/login", {"email": "owner@a.ae", "password": PW, "next": evil})
        assert r.status_code == 303 and r.headers["location"] == "/app", evil
    r = post(c, "/login", {"email": "owner@a.ae", "password": PW, "next": "/app#overview"})
    assert r.headers["location"] == "/app#overview"
    g = c.get("/login?next=https://evil.example")
    assert "evil.example" not in g.text


def test_reset_flow_and_bad_token_generic(env):
    c, mailer, _ = env
    signup(c, mailer)
    mailer.outbox.clear()
    assert post(c, "/forgot", {"email": "ghost@a.ae"}).headers["location"] == "/forgot?sent=1"
    assert not mailer.outbox
    assert post(c, "/forgot", {"email": "owner@a.ae"}).headers["location"] == "/forgot?sent=1"
    tok = link_token(mailer, "reset")
    r = post(c, "/reset", {"token": tok, "password": "a brand new passphrase"}, form_path=f"/reset?token={tok}")
    assert r.status_code == 303 and r.headers["location"] == "/login?reset=1"
    assert "password was changed" in c.get("/login?reset=1").text
    bad = post(c, "/reset", {"token": "bogus", "password": "a brand new passphrase"}, form_path="/reset?token=bogus")
    reuse = post(c, "/reset", {"token": tok, "password": "another new passphrase"}, form_path=f"/reset?token={tok}")
    assert bad.status_code == reuse.status_code == 400 and "not valid or has expired" in bad.text and "not valid or has expired" in reuse.text


def test_mfa_form_login_and_token_not_in_url(env):
    c, mailer, app = env
    signup(c, mailer)
    register_token = link_token(mailer, "verify")
    post(c, "/verify", {"token": register_token}, form_path=f"/verify?token={register_token}")
    post(c, "/login", {"email": "owner@a.ae", "password": PW})
    h = {"X-CSRF-Token": c.get("/auth/me").json()["csrf_token"]}
    setup = c.post("/auth/mfa/begin", headers=h).json()
    c.post("/auth/mfa/confirm", json={"code": totp_code(setup["secret"])}, headers=h)
    c2 = TestClient(app)
    r = post(c2, "/login", {"email": "owner@a.ae", "password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa" and "dbl_session" not in "".join(r.headers.get_list("set-cookie"))
    assert c2.get("/auth/me").status_code == 401
    bad = post(c2, "/login/mfa", {"code": "000000"})
    assert bad.status_code == 401 and "not valid" in bad.text
    ok = post(c2, "/login/mfa", {"code": totp_code(setup["secret"], __import__("time").time() + 30)})
    assert ok.status_code == 303 and ok.headers["location"] == "/app" and c2.get("/auth/me").status_code == 200
    assert TestClient(app).get("/login/mfa", follow_redirects=False).status_code == 303


def test_invite_page_flow(env):
    c, mailer, app = env
    signup(c, mailer)
    vt = link_token(mailer, "verify")
    post(c, "/verify", {"token": vt}, form_path=f"/verify?token={vt}")
    post(c, "/login", {"email": "owner@a.ae", "password": PW})
    h = {"X-CSRF-Token": c.get("/auth/me").json()["csrf_token"]}
    c.post("/auth/invite", json={"email": "mgr@a.ae", "role": "manager"}, headers=h)
    tok = link_token(mailer, "invite")
    m = TestClient(app)
    assert "<form" in m.get(f"/invite?token={tok}").text
    r = post(m, "/invite", {"token": tok, "password": PW, "name": "Mona"}, form_path=f"/invite?token={tok}")
    assert r.status_code == 303
    bad = post(m, "/invite", {"token": tok, "password": PW, "name": "Mona"}, form_path=f"/invite?token={tok}")
    assert bad.status_code == 400 and "not valid or has expired" in bad.text


def test_auth_pages_are_noindex_nostore_and_escape_input(env):
    c, mailer, _ = env
    r = c.get("/login")
    assert 'noindex' in r.text and "no-store" in r.headers["cache-control"] and "dbl_form=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    evil = '"><script>alert(1)</script>'
    p = post(c, "/login", {"email": evil, "password": "x"})
    assert "<script>alert(1)" not in p.text
    assert "<script>alert(1)" not in c.get("/reset", params={"token": evil}).text
    for path in ("/start", "/login", "/forgot"):
        assert "text/html" in c.get(path).headers["content-type"]


def test_signup_closed_in_invite_mode(tmp_path):
    app, _ = make_app(tmp_path, signup_mode="invite")
    c = TestClient(app)
    assert "invitation" in c.get("/start").text.lower()
    assert post(c, "/start", {"email": "a@a.ae", "password": PW, "name": "A", "agency_name": "X"}).status_code == 403
