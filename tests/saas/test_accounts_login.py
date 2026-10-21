import pytest

from leadengine.platform import accounts as acc_mod
from leadengine.platform.accounts import (SESSION_ABSOLUTE_S, SESSION_IDLE_S, VERIFY_TTL_S, AccountError, csrf_for)

from .conftest import PW, token_from


def reg(a, email="owner@agency.ae", pw=PW, **kw):
    return a.register(email, pw, kw.pop("name", "Aisha"), kw.pop("agency", "Acme Realty"), **kw)


def test_register_creates_user_tenant_owner_and_sends_verify_mail(accounts, control, mailer):
    r = reg(accounts, "Owner@Agency.ae")
    assert r.user_id > 0 and r.tenant_id > 0
    assert control.one("SELECT email, email_verified_at FROM users")["email"] == "owner@agency.ae"
    m = control.one("SELECT role FROM memberships WHERE user_id=? AND tenant_id=?", (r.user_id, r.tenant_id))
    assert m["role"] == "owner"
    assert len(mailer.outbox) == 1 and mailer.outbox[0]["to"] == "owner@agency.ae" and "https://app.example/verify?token=" in mailer.outbox[0]["text"]
    assert PW not in str(control.q("SELECT * FROM users")[0]["password_hash"])


def test_register_existing_email_is_indistinguishable(accounts, control, mailer):
    reg(accounts)
    mailer.outbox.clear()
    again = reg(accounts, "OWNER@agency.ae")
    assert type(again) is type(reg(accounts, "second@agency.ae")) and control.one("SELECT COUNT(*) n FROM users WHERE email LIKE 'owner@%'")["n"] == 1
    assert "verify?token=" not in mailer.outbox[0]["text"] and mailer.outbox[0]["to"] == "owner@agency.ae"


@pytest.mark.parametrize("email,pw,code", [("not-an-email", PW, "invalid_email"), ("a@b", PW, "invalid_email"), ("x" * 250 + "@a.com", PW, "invalid_email"),
                                           ("ok@agency.ae", "short", "weak_password"), ("ok@agency.ae", "a" * 300, "weak_password")])
def test_bad_input_rejected(accounts, email, pw, code):
    with pytest.raises(AccountError) as e:
        reg(accounts, email, pw)
    assert e.value.code == code


def test_register_is_rate_limited_per_ip(accounts):
    for i in range(5):
        reg(accounts, f"u{i}@agency.ae", ip="9.9.9.9")
    with pytest.raises(AccountError) as e:
        reg(accounts, "u9@agency.ae", ip="9.9.9.9")
    assert e.value.code == "rate_limited"
    reg(accounts, "u10@agency.ae", ip="8.8.8.8")


def test_verify_token_single_use_and_expires(accounts, control, mailer, clock):
    reg(accounts)
    t = token_from(mailer.outbox[0])
    assert accounts.verify_email(t) is True and control.one("SELECT email_verified_at FROM users")["email_verified_at"]
    assert accounts.verify_email(t) is False
    reg(accounts, "late@agency.ae")
    t2 = token_from(mailer.outbox[-1])
    clock.advance(VERIFY_TTL_S + 1)
    assert accounts.verify_email(t2) is False and accounts.verify_email("garbage") is False


def test_login_ok_returns_session_and_stores_only_hash(accounts, control):
    reg(accounts)
    r = accounts.login("owner@agency.ae", PW)
    assert r.status == "ok" and r.session_token and r.mfa_token is None
    assert control.one("SELECT COUNT(*) n FROM sessions WHERE token_hash=?", (r.session_token,))["n"] == 0
    info = accounts.resolve_session(r.session_token)
    assert info.role == "owner" and info.email == "owner@agency.ae" and info.email_verified is False and info.csrf_token == csrf_for(r.session_token)


def test_login_failures_are_uniform(accounts, clock):
    reg(accounts)
    shapes = set()
    for email, pw in [("nobody@agency.ae", PW), ("owner@agency.ae", "wrong-password-1")]:
        r = accounts.login(email, pw)
        shapes.add((r.status, r.session_token, r.mfa_token))
    for _ in range(5):
        accounts.login("owner@agency.ae", "wrong-password-1")
    locked = accounts.login("owner@agency.ae", PW)
    shapes.add((locked.status, locked.session_token, locked.mfa_token))
    assert shapes == {("failed", None, None)}


def test_unknown_email_still_runs_a_hash(accounts, monkeypatch):
    calls = []
    real = acc_mod.verify_password
    monkeypatch.setattr(acc_mod, "verify_password", lambda p, s: calls.append(1) or real(p, s))
    accounts.login("ghost@agency.ae", PW)
    assert len(calls) >= 1


def test_lockout_progression(accounts, clock):
    reg(accounts)
    for _ in range(5):
        assert accounts.login("owner@agency.ae", "wrong-password-1").status == "failed"
    assert accounts.login("owner@agency.ae", PW).status == "failed"          # locked 60 s even with the right password
    clock.advance(61)
    assert accounts.login("owner@agency.ae", PW).status == "ok"
    for _ in range(5):
        accounts.login("owner@agency.ae", "wrong-password-1")
    clock.advance(61)
    assert accounts.login("owner@agency.ae", "wrong-password-1").status == "failed"      # 6th consecutive failure doubled the lock to 120 s
    clock.advance(61)
    assert accounts.login("owner@agency.ae", PW).status == "failed"
    clock.advance(61)
    assert accounts.login("owner@agency.ae", PW).status == "ok"


def test_lockout_caps_at_one_hour(accounts, clock, control):
    reg(accounts)
    for i in range(40):
        accounts.login("owner@agency.ae", "wrong-password-1")
        if i < 39:
            clock.advance(3601)
    assert 0 < control.one("SELECT locked_until - ? AS rem FROM users", (clock(),))["rem"] <= 3600


def test_ip_attempt_limit_is_uniform(accounts):
    reg(accounts)
    for i in range(25):
        accounts.login(f"x{i}@agency.ae", "wrong-password-1", ip="7.7.7.7")
    assert accounts.login("owner@agency.ae", PW, ip="7.7.7.7").status == "failed"
    assert accounts.login("owner@agency.ae", PW, ip="6.6.6.6").status == "ok"


def test_session_rotation_on_login(accounts):
    reg(accounts)
    a = accounts.login("owner@agency.ae", PW).session_token
    b = accounts.login("owner@agency.ae", PW).session_token
    assert a != b and accounts.resolve_session(a) and accounts.resolve_session(b)


def test_idle_and_absolute_expiry(accounts, clock):
    reg(accounts)
    t = accounts.login("owner@agency.ae", PW).session_token
    clock.advance(SESSION_IDLE_S - 60)
    assert accounts.resolve_session(t)                                     # touch extends idle window
    clock.advance(SESSION_IDLE_S - 60)
    assert accounts.resolve_session(t)
    clock.advance(SESSION_IDLE_S + 60)
    assert accounts.resolve_session(t) is None                             # idle expiry
    t2 = accounts.login("owner@agency.ae", PW).session_token
    for _ in range(int(SESSION_ABSOLUTE_S // (SESSION_IDLE_S - 60)) + 2):
        clock.advance(SESSION_IDLE_S - 60)
        accounts.resolve_session(t2)
    assert accounts.resolve_session(t2) is None                            # absolute expiry despite activity


def test_email_case_and_whitespace_insensitive_login(accounts):
    reg(accounts)
    assert accounts.login("  OWNER@Agency.AE ", PW).status == "ok"


def test_logout_and_revoke_all(accounts):
    r = reg(accounts)
    a = accounts.login("owner@agency.ae", PW).session_token
    b = accounts.login("owner@agency.ae", PW).session_token
    accounts.logout(a)
    assert accounts.resolve_session(a) is None and accounts.resolve_session(b)
    accounts.revoke_all(r.user_id)
    assert accounts.resolve_session(b) is None
    assert accounts.resolve_session("") is None and accounts.resolve_session("x" * 500) is None


def test_session_info_reflects_verification(accounts, mailer):
    reg(accounts)
    accounts.verify_email(token_from(mailer.outbox[0]))
    assert accounts.resolve_session(accounts.login("owner@agency.ae", PW).session_token).email_verified is True


def test_audit_trail_has_no_secrets(accounts, control):
    reg(accounts)
    accounts.login("owner@agency.ae", PW)
    accounts.login("owner@agency.ae", "wrong-password-1")
    blob = " ".join(r["detail"] or "" for r in control.q("SELECT detail FROM audit"))
    assert PW not in blob and "wrong-password-1" not in blob
    assert {"register", "login", "login_failed"} <= {r["action"] for r in control.q("SELECT action FROM audit")}
