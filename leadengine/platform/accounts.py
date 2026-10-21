"""Account service: registration, email verification, login, sessions, lockout, password reset, TOTP MFA, invites and API keys.

Failure responses are uniform: unknown email, wrong password, locked account and IP throttling all return the same `LoginResult`,
and exactly one scrypt verification runs in every case so timing does not reveal which one it was."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from .control import ControlDB
from .mailer import Mailer
from .security import (Encryptor, check_password_policy, hash_password, hash_token, new_recovery_codes, new_token, new_totp_secret, otpauth_uri,
                       totp_matching_step, verify_password)

SESSION_IDLE_S = 12 * 3600
SESSION_ABSOLUTE_S = 30 * 86400
VERIFY_TTL_S = 24 * 3600
MFA_TTL_S = 300
LOCK_BASE_S, LOCK_MAX_S, LOCK_AFTER = 60, 3600, 5
IP_LOGIN_LIMIT, IP_LOGIN_WINDOW_S = 20, 600
REGISTER_LIMIT, REGISTER_WINDOW_S = 5, 3600
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

_dummy: str | None = None


def _dummy_hash() -> str:
    global _dummy
    if _dummy is None:
        _dummy = hash_password("dummy-password-used-only-for-timing")
    return _dummy


class AccountError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


@dataclass(frozen=True)
class RegisterResult:
    user_id: int
    tenant_id: int


@dataclass(frozen=True)
class LoginResult:
    status: str                       # ok | mfa_required | failed
    session_token: str | None = None
    mfa_token: str | None = None


@dataclass(frozen=True)
class SessionInfo:
    user_id: int
    tenant_id: int
    role: str
    agent_id: int | None
    mfa_ok: bool
    email_verified: bool
    csrf_token: str
    email: str
    name: str | None
    lang: str
    tenant_name: str


@dataclass(frozen=True)
class MfaSetup:
    secret: str
    uri: str


FAILED = LoginResult("failed")
INVITE_TTL_S = 7 * 86400
RESET_TTL_S = 3600
RESET_LIMIT, RESET_WINDOW_S = 5, 3600
ROLES_INVITABLE = ("manager", "agent")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def csrf_for(session_token: str) -> str:
    """CSRF token bound to the session (HMAC of the raw session token): another user's token never validates."""
    return hmac.new(session_token.encode(), b"dbl-csrf", hashlib.sha256).hexdigest()[:40]


class AccountService:
    def __init__(self, control: ControlDB, mailer: Mailer, tenants, settings: Settings, now=time.time,
                 encryptor: Encryptor | None = None, background=None):
        """`background(fn)` runs mail sends off the request path (a thread pool in production) so response time never reveals
        whether an email address has an account. Default: send inline."""
        self.control, self.mailer, self.tenants, self.s, self.now = control, mailer, tenants, settings, now
        self.encryptor, self.background = encryptor, background

    def _send(self, to: str, subject: str, text: str) -> None:
        if self.background:
            self.background(lambda: self.mailer.send(to, subject, text))
        else:
            self.mailer.send(to, subject, text)

    # ------------------------------------------------------------------ helpers
    def _count(self, key: str, window: float) -> int:
        return self.control.one("SELECT COUNT(*) n FROM login_attempts WHERE key=? AND ts>?", (key, self.now() - window))["n"]

    def _record(self, key: str) -> None:
        with self.control.tx() as c:
            c.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", (key, self.now()))
            c.execute("DELETE FROM login_attempts WHERE ts<?", (self.now() - 86400,))

    def _issue_token(self, user_id: int, kind: str, ttl: float) -> str:
        raw, h = new_token()
        with self.control.tx() as c:
            c.execute("INSERT INTO auth_tokens(token_hash,user_id,kind,expires_at) VALUES(?,?,?,?)", (h, user_id, kind, self.now() + ttl))
        return raw

    def _consume_token(self, raw: str, kind: str) -> int | None:
        """Single-use: returns the user id and marks the token used, or None if unknown/expired/used."""
        if not isinstance(raw, str) or not raw or len(raw) > 200:
            return None
        h = hash_token(raw)
        with self.control.tx() as c:
            row = c.execute("SELECT user_id FROM auth_tokens WHERE token_hash=? AND kind=? AND used_at IS NULL AND expires_at>?",
                            (h, kind, self.now())).fetchone()
            if not row:
                return None
            c.execute("UPDATE auth_tokens SET used_at=? WHERE token_hash=?", (self.now(), h))
            return row["user_id"]

    def _link(self, path: str, token: str) -> str:
        return f"{self.s.base_url.rstrip('/')}{path}?token={token}"

    # ------------------------------------------------------------------ registration
    def register(self, email: str, password: str, name: str, agency_name: str, lang: str = "en", ip: str = "") -> RegisterResult:
        email, name, agency_name = normalize_email(email), (name or "").strip(), (agency_name or "").strip()
        if self._count(f"reg:{ip}", REGISTER_WINDOW_S) >= REGISTER_LIMIT:
            raise AccountError("rate_limited", "Too many sign-ups from this network. Try again later.")
        if len(email) > 254 or not EMAIL_RE.match(email):
            raise AccountError("invalid_email", "Enter a valid email address.")
        problem = check_password_policy(password)
        if problem:
            raise AccountError("weak_password", problem)
        if not (1 <= len(name) <= 120 and 1 <= len(agency_name) <= 120):
            raise AccountError("invalid_input", "Name and agency name are required (max 120 characters).")
        self._record(f"reg:{ip}")
        pw_hash = hash_password(password)                       # computed for new and existing emails alike (equal timing)
        if self.control.one("SELECT 1 FROM users WHERE email=?", (email,)):
            self._send(email, "DigitalBurj Leads sign-up attempt",
                             "Someone tried to create a DigitalBurj Leads account with this address. If it was you, sign in or reset your "
                             f"password at {self.s.base_url.rstrip('/')}/login. If not, ignore this email.")
            return RegisterResult(0, 0)
        tenant = self.tenants.create_tenant(agency_name)
        with self.control.tx() as c:
            uid = c.execute("INSERT INTO users(email,password_hash,name,lang,created_at) VALUES(?,?,?,?,?)",
                            (email, pw_hash, name, lang if lang in ("en", "ar") else "en", _iso(self.now()))).lastrowid
            c.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(?,?,'owner')", (uid, tenant.id))
        token = self._issue_token(uid, "verify", VERIFY_TTL_S)
        self._send(email, "Verify your DigitalBurj Leads email",
                         f"Welcome to DigitalBurj Leads.\n\nConfirm your email address: {self._link('/verify', token)}\n\nThe link works once and expires in 24 hours.")
        self.control.audit("register", user_id=uid, tenant_id=tenant.id, ip=ip)
        return RegisterResult(uid, tenant.id)

    def verify_email(self, raw_token: str) -> bool:
        uid = self._consume_token(raw_token, "verify")
        if uid is None:
            return False
        with self.control.tx() as c:
            c.execute("UPDATE users SET email_verified_at=? WHERE id=? AND email_verified_at IS NULL", (_iso(self.now()), uid))
        self.control.audit("email_verified", user_id=uid)
        return True

    # ------------------------------------------------------------------ login / sessions
    def login(self, email: str, password: str, ip: str = "") -> LoginResult:
        email, now = normalize_email(email), self.now()
        user = self.control.one("SELECT * FROM users WHERE email=?", (email,)) if EMAIL_RE.match(email) and len(email) <= 254 else None
        ip_blocked = self._count(f"ip:{ip}", IP_LOGIN_WINDOW_S) >= IP_LOGIN_LIMIT
        ok = verify_password(password if isinstance(password, str) else "", user["password_hash"] if user and user["password_hash"] else _dummy_hash())
        locked = bool(user and user["locked_until"] > now)
        if not (user and ok) or locked or ip_blocked:
            self._record(f"ip:{ip}")
            if user and not ok and not locked:
                self._fail(user, ip)
            else:
                self.control.audit("login_failed", user_id=user["id"] if user else None, ip=ip, reason="blocked" if (locked or ip_blocked) else "unknown")
            return FAILED
        with self.control.tx() as c:
            c.execute("UPDATE users SET failed_logins=0, locked_until=0 WHERE id=?", (user["id"],))
        if user["totp_enabled"]:
            return LoginResult("mfa_required", None, self._issue_token(user["id"], "mfa", MFA_TTL_S))
        return LoginResult("ok", self._new_session(user["id"], ip), None)

    def _fail(self, user, ip: str) -> None:
        n = user["failed_logins"] + 1
        lock = min(LOCK_BASE_S * 2 ** (n - LOCK_AFTER), LOCK_MAX_S) if n >= LOCK_AFTER else 0
        with self.control.tx() as c:
            c.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (n, self.now() + lock if lock else 0, user["id"]))
        self.control.audit("login_failed", user_id=user["id"], ip=ip, reason="password", failures=n)

    def _new_session(self, user_id: int, ip: str = "") -> str:
        m = self.control.one("SELECT tenant_id FROM memberships WHERE user_id=? ORDER BY tenant_id LIMIT 1", (user_id,))
        if not m:
            raise AccountError("no_workspace", "This account has no workspace.")
        raw, h = new_token()
        now = self.now()
        with self.control.tx() as c:
            c.execute("INSERT INTO sessions(token_hash,user_id,tenant_id,mfa_ok,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?)",
                      (h, user_id, m["tenant_id"], 1, now, now, now + SESSION_ABSOLUTE_S))
        self.control.audit("login", user_id=user_id, tenant_id=m["tenant_id"], ip=ip)
        return raw

    def resolve_session(self, raw_token: str, touch: bool = True) -> SessionInfo | None:
        if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 200:
            return None
        h, now = hash_token(raw_token), self.now()
        row = self.control.one(
            "SELECT s.*, m.role, m.agent_id, u.email, u.name, u.lang, u.email_verified_at, t.name tenant_name, t.status "
            "FROM sessions s JOIN memberships m ON m.user_id=s.user_id AND m.tenant_id=s.tenant_id JOIN users u ON u.id=s.user_id "
            "JOIN tenants t ON t.id=s.tenant_id WHERE s.token_hash=?", (h,))
        if not row:
            return None
        if now >= row["expires_at"] or now - row["last_seen"] >= SESSION_IDLE_S or row["status"] != "active":
            with self.control.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
            return None
        if touch:
            with self.control.tx() as c:
                c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, h))
        return SessionInfo(row["user_id"], row["tenant_id"], row["role"], row["agent_id"], bool(row["mfa_ok"]), bool(row["email_verified_at"]),
                           csrf_for(raw_token), row["email"], row["name"], row["lang"] or "en", row["tenant_name"])

    def logout(self, raw_token: str) -> None:
        with self.control.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(raw_token or ""),))

    def revoke_all(self, user_id: int) -> None:
        with self.control.tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.control.audit("sessions_revoked", user_id=user_id)


    # ------------------------------------------------------------------ password reset / change
    def request_reset(self, email: str, ip: str = "") -> None:
        """Always returns None. Mail goes out only for known addresses, deferred when a background runner is set."""
        email = normalize_email(email)
        if self._count(f"reset:{ip}", RESET_WINDOW_S) >= RESET_LIMIT:
            return None
        self._record(f"reset:{ip}")
        user = self.control.one("SELECT id FROM users WHERE email=?", (email,)) if EMAIL_RE.match(email) else None
        if user:
            token = self._issue_token(user["id"], "reset", RESET_TTL_S)
            self._send(email, "Reset your DigitalBurj Leads password",
                       f"Choose a new password: {self._link('/reset', token)}\n\nThe link works once and expires in 1 hour. "
                       "If you did not ask for this, ignore this email.")
            self.control.audit("reset_requested", user_id=user["id"], ip=ip)
        return None

    def _set_password(self, user_id: int, new_password: str, keep_session: str | None = None) -> None:
        h = hash_password(new_password)
        with self.control.tx() as c:
            c.execute("UPDATE users SET password_hash=?, failed_logins=0, locked_until=0 WHERE id=?", (h, user_id))
            c.execute("UPDATE auth_tokens SET used_at=? WHERE user_id=? AND kind='reset' AND used_at IS NULL", (self.now(), user_id))
            if keep_session:
                c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, hash_token(keep_session)))
            else:
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def reset_password(self, raw_token: str, new_password: str) -> bool:
        problem = check_password_policy(new_password)
        if problem:                                           # checked first so a typo does not burn the one-time token
            raise AccountError("weak_password", problem)
        uid = self._consume_token(raw_token, "reset")
        if uid is None:
            return False
        self._set_password(uid, new_password)
        self.control.audit("password_reset", user_id=uid)
        return True

    def change_password(self, user_id: int, old_password: str, new_password: str, keep_session: str | None = None) -> bool:
        problem = check_password_policy(new_password)
        if problem:
            raise AccountError("weak_password", problem)
        row = self.control.one("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if not row or not verify_password(old_password, row["password_hash"] or _dummy_hash()):
            return False
        self._set_password(user_id, new_password, keep_session)
        self.control.audit("password_changed", user_id=user_id)
        return True

    # ------------------------------------------------------------------ MFA (TOTP)
    def _enc(self) -> Encryptor:
        if self.encryptor is None:
            raise AccountError("mfa_unavailable", "Two-factor authentication needs ENCRYPTION_KEY to be configured.")
        return self.encryptor

    def mfa_begin(self, user_id: int) -> MfaSetup:
        enc, user = self._enc(), self.control.one("SELECT email, totp_enabled FROM users WHERE id=?", (user_id,))
        if not user:
            raise AccountError("not_found")
        if user["totp_enabled"]:
            raise AccountError("mfa_enabled", "Two-factor authentication is already on.")
        secret = new_totp_secret()
        with self.control.tx() as c:
            c.execute("UPDATE users SET totp_secret_enc=?, totp_last_step=0 WHERE id=?", (enc.encrypt(secret), user_id))
        return MfaSetup(secret, otpauth_uri(secret, user["email"]))

    def mfa_confirm(self, user_id: int, code: str) -> list[str]:
        enc, user = self._enc(), self.control.one("SELECT totp_secret_enc, totp_enabled FROM users WHERE id=?", (user_id,))
        if not user or not user["totp_secret_enc"] or user["totp_enabled"]:
            raise AccountError("invalid_state", "Start two-factor setup first.")
        step = totp_matching_step(enc.decrypt(user["totp_secret_enc"]), code, self.now())
        if step is None:
            raise AccountError("invalid_code", "That code is not valid.")
        codes = new_recovery_codes()
        with self.control.tx() as c:
            c.execute("UPDATE users SET totp_enabled=1, totp_last_step=? WHERE id=?", (step, user_id))
            c.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
            c.executemany("INSERT INTO recovery_codes(user_id,code_hash) VALUES(?,?)", [(user_id, hash_token(x)) for x in codes])
        self.control.audit("mfa_enabled", user_id=user_id)
        return codes

    def mfa_disable(self, user_id: int, password: str) -> bool:
        row = self.control.one("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if not row or not verify_password(password, row["password_hash"] or _dummy_hash()):
            return False
        with self.control.tx() as c:
            c.execute("UPDATE users SET totp_enabled=0, totp_secret_enc=NULL, totp_last_step=0 WHERE id=?", (user_id,))
            c.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        self.control.audit("mfa_disabled", user_id=user_id)
        return True

    def login_mfa(self, mfa_token: str, code: str, ip: str = "") -> LoginResult:
        """Second factor. A wrong code does not consume the token but counts toward the account lockout."""
        h = hash_token(mfa_token or "") if isinstance(mfa_token, str) and len(mfa_token) <= 200 else ""
        row = self.control.one("SELECT user_id FROM auth_tokens WHERE token_hash=? AND kind='mfa' AND used_at IS NULL AND expires_at>?", (h, self.now()))
        user = self.control.one("SELECT * FROM users WHERE id=?", (row["user_id"],)) if row else None
        code = (code or "").strip().lower() if isinstance(code, str) else ""
        if not user or user["locked_until"] > self.now() or not user["totp_enabled"] or self._count(f"ip:{ip}", IP_LOGIN_WINDOW_S) >= IP_LOGIN_LIMIT:
            self._record(f"ip:{ip}")
            return FAILED
        ok = False
        if len(code) == 6 and code.isdigit():
            step = totp_matching_step(self._enc().decrypt(user["totp_secret_enc"]), code, self.now())
            if step is not None and step > user["totp_last_step"]:           # a time-step can be used only once
                with self.control.tx() as c:
                    ok = c.execute("UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step<?", (step, user["id"], step)).rowcount == 1
        elif re.fullmatch(r"[a-z0-9]{5}-[a-z0-9]{5}", code):
            with self.control.tx() as c:
                ok = c.execute("UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                               (self.now(), user["id"], hash_token(code))).rowcount == 1
        if not ok:
            self._record(f"ip:{ip}")
            self._fail(user, ip)
            return FAILED
        self._consume_token(mfa_token, "mfa")
        with self.control.tx() as c:
            c.execute("UPDATE users SET failed_logins=0, locked_until=0 WHERE id=?", (user["id"],))
        return LoginResult("ok", self._new_session(user["id"], ip), None)

    # ------------------------------------------------------------------ team invites
    def _role_of(self, user_id: int, tenant_id: int) -> str | None:
        m = self.control.one("SELECT role FROM memberships WHERE user_id=? AND tenant_id=?", (user_id, tenant_id))
        return m["role"] if m else None

    def invite(self, tenant_id: int, inviter_id: int, email: str, role: str, agent_id: int | None = None) -> str:
        if self._role_of(inviter_id, tenant_id) != "owner":
            raise AccountError("forbidden", "Only the workspace owner can invite people.")
        inviter = self.control.one("SELECT email_verified_at FROM users WHERE id=?", (inviter_id,))
        if not inviter or not inviter["email_verified_at"]:
            raise AccountError("not_verified", "Verify your email address before inviting people.")
        email = normalize_email(email)
        if role not in ROLES_INVITABLE:
            raise AccountError("invalid_role", "Invite as manager or agent.")
        if len(email) > 254 or not EMAIL_RE.match(email):
            raise AccountError("invalid_email", "Enter a valid email address.")
        raw, h = new_token()
        with self.control.tx() as c:
            c.execute("INSERT INTO invites(token_hash,tenant_id,email,role,agent_id,expires_at) VALUES(?,?,?,?,?,?)",
                      (h, tenant_id, email, role, agent_id, self.now() + INVITE_TTL_S))
        t = self.control.one("SELECT name FROM tenants WHERE id=?", (tenant_id,))
        self.mailer.send(email, f"You are invited to {t['name']} on DigitalBurj Leads",
                         f"You have been invited to join {t['name']} as {role}.\n\nAccept: {self._link('/invite', raw)}\n\nThe link expires in 7 days.")
        self.control.audit("invite_sent", user_id=inviter_id, tenant_id=tenant_id, invited_role=role)
        return raw

    def accept_invite(self, raw_token: str, password: str, name: str) -> int:
        bad = AccountError("invalid_invite", "This invitation is not valid or has expired.")
        if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 200:
            raise bad
        inv = self.control.one("SELECT * FROM invites WHERE token_hash=? AND accepted_at IS NULL AND expires_at>?", (hash_token(raw_token), self.now()))
        if not inv:
            raise bad
        user = self.control.one("SELECT * FROM users WHERE email=?", (inv["email"],))
        if user:
            if not verify_password(password, user["password_hash"] or _dummy_hash()):
                raise AccountError("invalid_credentials", "Enter the password of your existing account.")
            if self._role_of(user["id"], inv["tenant_id"]):
                raise AccountError("already_member", "You are already a member of this workspace.")
            uid = user["id"]
        else:
            problem = check_password_policy(password)
            if problem:
                raise AccountError("weak_password", problem)
            if not 1 <= len((name or "").strip()) <= 120:
                raise AccountError("invalid_input", "Name is required.")
            with self.control.tx() as c:
                uid = c.execute("INSERT INTO users(email,password_hash,name,email_verified_at,created_at) VALUES(?,?,?,?,?)",
                                (inv["email"], hash_password(password), name.strip(), _iso(self.now()), _iso(self.now()))).lastrowid
        with self.control.tx() as c:
            c.execute("INSERT INTO memberships(user_id,tenant_id,role,agent_id) VALUES(?,?,?,?)", (uid, inv["tenant_id"], inv["role"], inv["agent_id"]))
            c.execute("UPDATE invites SET accepted_at=? WHERE token_hash=?", (self.now(), inv["token_hash"]))
        self.control.audit("invite_accepted", user_id=uid, tenant_id=inv["tenant_id"], role=inv["role"])
        return uid

    # ------------------------------------------------------------------ workspace API key
    def rotate_api_key(self, tenant_id: int, actor_id: int) -> str:
        if self._role_of(actor_id, tenant_id) != "owner":
            raise AccountError("forbidden", "Only the workspace owner can rotate the API key.")
        raw = "dbl_" + new_token()[0]
        with self.control.tx() as c:
            c.execute("UPDATE tenants SET api_key_hash=? WHERE id=?", (hash_token(raw), tenant_id))
        self.control.audit("api_key_rotated", user_id=actor_id, tenant_id=tenant_id)
        return raw

    def verify_api_key(self, raw: str) -> int | None:
        if not isinstance(raw, str) or not raw.startswith("dbl_") or len(raw) > 200:
            return None
        row = self.control.one("SELECT id FROM tenants WHERE api_key_hash=? AND status='active'", (hash_token(raw),))
        return row["id"] if row else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
