import pytest

from leadengine.platform.accounts import AccountError, VERIFY_TTL_S
from leadengine.platform.security import totp_code

from .conftest import PW, token_from

NEW = "an even better passphrase"


def mk(a, mailer, email="owner@agency.ae", verified=True):
    r = a.register(email, PW, "Aisha", "Acme Realty")
    if verified:
        a.verify_email(token_from(mailer.outbox[-1]))
    mailer.outbox.clear()
    return r


# ---------------------------------------------------------------- reset / change password
def test_reset_flow_single_use_expiry_and_session_revocation(accounts, mailer, clock):
    r = mk(accounts, mailer)
    s = accounts.login("owner@agency.ae", PW).session_token
    accounts.request_reset("owner@agency.ae")
    assert "reset?token=" in mailer.outbox[0]["text"]
    tok = token_from(mailer.outbox[0])
    assert accounts.reset_password(tok, NEW) is True
    assert accounts.resolve_session(s) is None                                   # every session revoked
    assert accounts.login("owner@agency.ae", PW).status == "failed" and accounts.login("owner@agency.ae", NEW).status == "ok"
    assert accounts.reset_password(tok, "yet another passphrase") is False       # single use
    accounts.request_reset("owner@agency.ae")
    late = token_from(mailer.outbox[-1])
    clock.advance(3601)
    assert accounts.reset_password(late, NEW) is False                           # 1 h expiry
    assert r.user_id


def test_reset_with_weak_password_does_not_burn_the_token(accounts, mailer):
    mk(accounts, mailer)
    accounts.request_reset("owner@agency.ae")
    tok = token_from(mailer.outbox[0])
    with pytest.raises(AccountError) as e:
        accounts.reset_password(tok, "short")
    assert e.value.code == "weak_password" and accounts.reset_password(tok, NEW) is True


def test_reset_request_identical_for_unknown_email(accounts, mailer):
    mk(accounts, mailer)
    assert accounts.request_reset("owner@agency.ae") is None and accounts.request_reset("ghost@agency.ae") is None
    assert [m["to"] for m in mailer.outbox] == ["owner@agency.ae"]


def test_reset_requests_are_rate_limited_silently(accounts, mailer):
    mk(accounts, mailer)
    for _ in range(8):
        assert accounts.request_reset("owner@agency.ae", ip="5.5.5.5") is None
    assert len(mailer.outbox) == 5


def test_old_reset_token_dead_after_password_change(accounts, mailer):
    r = mk(accounts, mailer)
    accounts.request_reset("owner@agency.ae")
    old = token_from(mailer.outbox[0])
    assert accounts.change_password(r.user_id, PW, NEW) is True
    assert accounts.reset_password(old, "third passphrase here") is False


def test_change_password_requires_old_and_revokes_others(accounts, mailer):
    r = mk(accounts, mailer)
    keep = accounts.login("owner@agency.ae", PW).session_token
    other = accounts.login("owner@agency.ae", PW).session_token
    assert accounts.change_password(r.user_id, "wrong-old-password", NEW, keep_session=keep) is False
    assert accounts.change_password(r.user_id, PW, NEW, keep_session=keep) is True
    assert accounts.resolve_session(keep) and accounts.resolve_session(other) is None
    with pytest.raises(AccountError):
        accounts.change_password(r.user_id, NEW, "short")


# ---------------------------------------------------------------- MFA
def enrol(accounts, uid, clock):
    setup = accounts.mfa_begin(uid)
    codes = accounts.mfa_confirm(uid, totp_code(setup.secret, clock()))
    return setup.secret, codes


def test_mfa_enrol_confirm_login_roundtrip(accounts, mailer, clock, control):
    r = mk(accounts, mailer)
    setup = accounts.mfa_begin(r.user_id)
    assert setup.uri.startswith("otpauth://totp/") and control.one("SELECT totp_enabled, totp_secret_enc FROM users")["totp_enabled"] == 0
    assert setup.secret not in control.one("SELECT totp_secret_enc FROM users")["totp_secret_enc"]       # encrypted at rest
    with pytest.raises(AccountError):
        accounts.mfa_confirm(r.user_id, "000000")
    codes = accounts.mfa_confirm(r.user_id, totp_code(setup.secret, clock()))
    assert len(codes) == 10
    clock.advance(30)
    first = accounts.login("owner@agency.ae", PW)
    assert first.status == "mfa_required" and first.session_token is None and first.mfa_token
    done = accounts.login_mfa(first.mfa_token, totp_code(setup.secret, clock()))
    assert done.status == "ok" and accounts.resolve_session(done.session_token).mfa_ok is True


def test_mfa_replay_rejected(accounts, mailer, clock):
    r = mk(accounts, mailer)
    secret, _ = enrol(accounts, r.user_id, clock)
    clock.advance(30)
    code = totp_code(secret, clock())
    assert accounts.login_mfa(accounts.login("owner@agency.ae", PW).mfa_token, code).status == "ok"
    assert accounts.login_mfa(accounts.login("owner@agency.ae", PW).mfa_token, code).status == "failed"     # same step cannot be reused


def test_wrong_mfa_codes_count_toward_lockout(accounts, mailer, clock):
    r = mk(accounts, mailer)
    enrol(accounts, r.user_id, clock)
    clock.advance(30)
    mfa = accounts.login("owner@agency.ae", PW).mfa_token
    for _ in range(5):
        assert accounts.login_mfa(mfa, "000000").status == "failed"
    assert accounts.login("owner@agency.ae", PW).status == "failed"            # account now locked


def test_mfa_token_is_single_use_and_expires(accounts, mailer, clock):
    r = mk(accounts, mailer)
    secret, _ = enrol(accounts, r.user_id, clock)
    clock.advance(30)
    mfa = accounts.login("owner@agency.ae", PW).mfa_token
    clock.advance(301)
    assert accounts.login_mfa(mfa, totp_code(secret, clock())).status == "failed"
    mfa2 = accounts.login("owner@agency.ae", PW).mfa_token
    assert accounts.login_mfa(mfa2, totp_code(secret, clock())).status == "ok"
    assert accounts.login_mfa(mfa2, totp_code(secret, clock() + 30)).status == "failed"


def test_recovery_code_single_use(accounts, mailer, clock, control):
    r = mk(accounts, mailer)
    _, codes = enrol(accounts, r.user_id, clock)
    assert all(c not in str(control.q("SELECT code_hash FROM recovery_codes")) for c in codes)
    assert accounts.login_mfa(accounts.login("owner@agency.ae", PW).mfa_token, codes[0]).status == "ok"
    assert accounts.login_mfa(accounts.login("owner@agency.ae", PW).mfa_token, codes[0]).status == "failed"
    assert accounts.login_mfa(accounts.login("owner@agency.ae", PW).mfa_token, codes[1].upper() + " ").status == "ok"


def test_mfa_disable_needs_password(accounts, mailer, clock):
    r = mk(accounts, mailer)
    enrol(accounts, r.user_id, clock)
    assert accounts.mfa_disable(r.user_id, "wrong-password-1") is False
    assert accounts.mfa_disable(r.user_id, PW) is True
    assert accounts.login("owner@agency.ae", PW).status == "ok"


def test_mfa_begin_refused_when_already_enabled(accounts, mailer, clock):
    r = mk(accounts, mailer)
    enrol(accounts, r.user_id, clock)
    with pytest.raises(AccountError):
        accounts.mfa_begin(r.user_id)


# ---------------------------------------------------------------- invites
def test_unverified_owner_cannot_invite(accounts, mailer):
    r = mk(accounts, mailer, verified=False)
    with pytest.raises(AccountError) as e:
        accounts.invite(r.tenant_id, r.user_id, "new@agency.ae", "agent")
    assert e.value.code == "not_verified"


def test_only_owner_can_invite_and_never_as_owner(accounts, mailer):
    r = mk(accounts, mailer)
    tok = accounts.invite(r.tenant_id, r.user_id, "mgr@agency.ae", "manager")
    mgr = accounts.accept_invite(tok, PW, "Mona")
    for who, role in ((mgr, "agent"), (r.user_id, "owner"), (r.user_id, "root")):
        with pytest.raises(AccountError):
            accounts.invite(r.tenant_id, who, "x@agency.ae", role)


def test_invite_accept_creates_membership_with_role_and_expires(accounts, mailer, control, clock):
    r = mk(accounts, mailer)
    tok = accounts.invite(r.tenant_id, r.user_id, "Agent@Agency.ae", "agent", agent_id=7)
    assert "invite?token=" in mailer.outbox[0]["text"] and mailer.outbox[0]["to"] == "agent@agency.ae"
    uid = accounts.accept_invite(tok, PW, "Omar")
    m = control.one("SELECT role, agent_id FROM memberships WHERE user_id=?", (uid,))
    assert (m["role"], m["agent_id"]) == ("agent", 7) and control.one("SELECT email_verified_at FROM users WHERE id=?", (uid,))["email_verified_at"]
    with pytest.raises(AccountError):
        accounts.accept_invite(tok, PW, "Omar")                                  # single use
    tok2 = accounts.invite(r.tenant_id, r.user_id, "late@agency.ae", "agent")
    clock.advance(7 * 86400 + 1)
    with pytest.raises(AccountError):
        accounts.accept_invite(tok2, PW, "Late")


def test_invite_for_existing_user_adds_membership_only_with_their_password(accounts, mailer, control):
    a = mk(accounts, mailer, "one@agency.ae")
    b = mk(accounts, mailer, "two@other.ae")
    tok = accounts.invite(a.tenant_id, a.user_id, "two@other.ae", "manager")
    with pytest.raises(AccountError):
        accounts.accept_invite(tok, "not-their-password", "Two")
    assert accounts.accept_invite(tok, PW, "Two") == b.user_id
    assert control.one("SELECT role FROM memberships WHERE user_id=? AND tenant_id=?", (b.user_id, a.tenant_id))["role"] == "manager"
    tok2 = accounts.invite(a.tenant_id, a.user_id, "two@other.ae", "agent")
    with pytest.raises(AccountError) as e:
        accounts.accept_invite(tok2, PW, "Two")
    assert e.value.code == "already_member"


# ---------------------------------------------------------------- API keys
def test_api_key_rotation_invalidates_previous_and_stores_hash(accounts, mailer, control):
    r = mk(accounts, mailer)
    k1 = accounts.rotate_api_key(r.tenant_id, r.user_id)
    assert k1.startswith("dbl_") and k1 not in str(control.q("SELECT * FROM tenants")[0]["api_key_hash"]) and accounts.verify_api_key(k1) == r.tenant_id
    k2 = accounts.rotate_api_key(r.tenant_id, r.user_id)
    assert k1 != k2 and accounts.verify_api_key(k1) is None and accounts.verify_api_key(k2) == r.tenant_id
    assert accounts.verify_api_key("") is None and accounts.verify_api_key("dbl_nope") is None


def test_only_owner_rotates_api_key(accounts, mailer):
    r = mk(accounts, mailer)
    mgr = accounts.accept_invite(accounts.invite(r.tenant_id, r.user_id, "m@agency.ae", "manager"), PW, "M")
    with pytest.raises(AccountError):
        accounts.rotate_api_key(r.tenant_id, mgr)


def test_deferred_mail_is_used_for_reset(control, mailer, clock):
    from leadengine.config import Settings
    from leadengine.platform.accounts import AccountService
    from .conftest import FakeTenants
    jobs = []
    a = AccountService(control, mailer, FakeTenants(control), Settings(db_path=":memory:"), now=clock, background=jobs.append)
    a.register("owner@agency.ae", PW, "A", "Acme")
    assert len(jobs) == 1 and not mailer.outbox                                  # sign-up mail is deferred too (no timing oracle)
    jobs.clear()
    assert VERIFY_TTL_S and a.request_reset("owner@agency.ae") is None
    assert len(jobs) == 1 and all("reset?token" not in m["text"] for m in mailer.outbox)       # mail only sent when the job runs
    jobs[0]()
    assert any("reset?token" in m["text"] for m in mailer.outbox)
