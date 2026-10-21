"""Regression tests for the Task 1-8 security checkpoint review."""
import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

from leadengine.config import Settings
from leadengine.platform.accounts import AccountError, AccountService
from leadengine.platform.security import generate_key

from .conftest import PW
from .test_app_auth import link_token, login, make_app, register, signed_in


def test_workspace_does_not_inherit_operator_env_secrets(tmp_path):
    from leadengine.platform.control import ControlDB
    from leadengine.platform.security import Encryptor
    from leadengine.platform.tenancy import TenantManager
    base = Settings(db_path=":memory:", control_db=":memory:", data_dir=str(tmp_path), encryption_key=generate_key(), form_webhook_secret="OPERATOR_SECRET",
                    meta_app_secret="OP_META", meta_verify_token="OP_VERIFY", meta_page_token="OP_PAGE", whatsapp_token="OP_WA", whatsapp_phone_id="OP_PHONE",
                    whatsapp_send_enabled=True, reddit_client_id="OP_RID", reddit_client_secret="OP_RSEC", apify_token="OP_APIFY",
                    capture_origins=["https://operator.example"], dnc_file="/etc/operator.dnc", brn="OP-BRN", trakheesi="OP-TRK",
                    agent_name="Operator Person", agency_name="Operator Agency")
    mgr = TenantManager(ControlDB(":memory:"), str(tmp_path), base, Encryptor(base.encryption_key))
    t = mgr.create_tenant("Victim Realty")
    s = mgr.settings_for(t.id)
    blob = repr(s)
    for leaked in ("OPERATOR_SECRET", "OP_META", "OP_VERIFY", "OP_PAGE", "OP_WA", "OP_PHONE", "OP_RID", "OP_RSEC", "OP_APIFY", "operator.example", "operator.dnc", "OP-BRN", "OP-TRK", "Operator Person", "Operator Agency"):
        assert leaked not in blob, leaked
    assert s.whatsapp_send_enabled is False and s.agency_name == "Victim Realty"
    mgr.set_setting(t.id, "form_webhook_secret", "mine")
    assert mgr.settings_for(t.id).form_webhook_secret == "mine"


def test_oversized_body_rejected_before_buffering_everything(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)

    def chunks():
        for _ in range(64):
            yield b"x" * 4096                     # 256 KiB chunked, no Content-Length

    r = c.post("/auth/login", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_tenant_api_body_limit(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    big = b"a,b\n" + b"x," * 10_000_000
    assert c.post("/ingest/csv", content=big, headers={**h, "content-type": "text/csv"}).status_code == 413


def test_public_tenant_body_limit(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    key = c.get("/auth/me").json()["workspace"]["public_key"]
    assert TestClient(app).post(f"/t/{key}/capture", content=b"{" + b" " * 400_000 + b"}", headers={"content-type": "application/json"}).status_code == 413


def test_failed_logins_count_exactly_under_concurrency(control, mailer, clock):
    from .conftest import FakeTenants
    svc = AccountService(control, mailer, FakeTenants(control), Settings(base_url="http://x"), now=clock)
    svc.register("a@a.ae", PW, "A", "Acme")
    import threading
    ts = [threading.Thread(target=lambda i=i: svc.login("a@a.ae", "wrong-password-xx", ip=f"1.1.1.{i}")) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    row = control.one("SELECT failed_logins n, locked_until l FROM users WHERE email='a@a.ae'")
    assert row["n"] >= 5 and row["l"] > clock()          # parallel failures must trip the lock (lost updates left it at 1)


def test_forgot_and_register_respond_in_comparable_time(tmp_path):
    from leadengine.platform import app as appmod
    app, mailer = make_app(tmp_path)
    c = TestClient(app)
    register(c, mailer, "known@a.ae")
    t0 = time.perf_counter(); c.post("/auth/forgot", json={"email": "known@a.ae"}); known = time.perf_counter() - t0
    t0 = time.perf_counter(); c.post("/auth/forgot", json={"email": "nobody@a.ae"}); unknown = time.perf_counter() - t0
    assert known >= appmod.FORGOT_FLOOR_S * 0.9 and unknown >= appmod.FORGOT_FLOOR_S * 0.9
    t0 = time.perf_counter(); c.post("/auth/register", json={"email": "known@a.ae", "password": PW, "name": "A", "agency_name": "X"}); dup = time.perf_counter() - t0
    assert dup >= appmod.REGISTER_FLOOR_S * 0.9


def test_console_mailer_does_not_log_links(caplog, mailer):
    with caplog.at_level(logging.DEBUG, logger="leadengine.platform.mailer"):
        mailer.send("a@a.ae", "Reset", "Choose a new password: http://x/reset?token=SECRETTOKEN")
    assert "SECRETTOKEN" not in caplog.text


def test_duplicate_session_cookies_are_refused(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    tok = c.cookies.get("dbl_session")
    ok = TestClient(app).get("/leads", headers={"Cookie": f"dbl_session={tok}"})
    assert ok.status_code == 200
    dup = TestClient(app).get("/leads", headers={"Cookie": f"dbl_session=junk; dbl_session={tok}"})
    assert dup.status_code == 401
    assert TestClient(app).get("/auth/me", headers={"Cookie": f"dbl_session=junk; dbl_session={tok}"}).status_code == 401


def test_password_checks_by_session_holder_are_throttled(control, mailer, clock):
    from .conftest import FakeTenants
    svc = AccountService(control, mailer, FakeTenants(control), Settings(base_url="http://x"), now=clock)
    uid = svc.register("a@a.ae", PW, "A", "Acme")
    uid = control.one("SELECT id FROM users")["id"]
    for _ in range(10):
        assert svc.change_password(uid, "wrong-password-xx", "another long passphrase") is False
    with pytest.raises(AccountError) as e:
        svc.change_password(uid, PW, "another long passphrase")
    assert e.value.code == "rate_limited"
    with pytest.raises(AccountError):
        svc.mfa_disable(uid, PW)


def test_login_for_user_without_workspace_is_uniform_failure(control, mailer, clock):
    from .conftest import FakeTenants
    svc = AccountService(control, mailer, FakeTenants(control), Settings(base_url="http://x"), now=clock)
    svc.register("a@a.ae", PW, "A", "Acme")
    with control.tx() as c:
        c.execute("DELETE FROM memberships")
    assert svc.login("a@a.ae", PW).status == "failed"
