"""Account service: registration, email verification, login, sessions, lockout. (Reset, MFA and invites are added in Task 5.)

Failure responses are uniform: unknown email, wrong password, locked account and IP throttling all return the same `LoginResult`,
and exactly one scrypt verification runs in every case so timing does not reveal which one it was."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from ..config import Settings
from .control import ControlDB
from .mailer import Mailer
from .security import check_password_policy, hash_password, hash_token, new_token, verify_password

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


FAILED = LoginResult("failed")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def csrf_for(session_token: str) -> str:
    """CSRF token bound to the session (HMAC of the raw session token): another user's token never validates."""
    return hmac.new(session_token.encode(), b"dbl-csrf", hashlib.sha256).hexdigest()[:40]


class AccountService:
    def __init__(self, control: ControlDB, mailer: Mailer, tenants, settings: Settings, now=time.time):
        self.control, self.mailer, self.tenants, self.s, self.now = control, mailer, tenants, settings, now

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
            self.mailer.send(email, "DigitalBurj Leads sign-up attempt",
                             "Someone tried to create a DigitalBurj Leads account with this address. If it was you, sign in or reset your "
                             f"password at {self.s.base_url.rstrip('/')}/login. If not, ignore this email.")
            return RegisterResult(0, 0)
        tenant = self.tenants.create_tenant(agency_name)
        with self.control.tx() as c:
            uid = c.execute("INSERT INTO users(email,password_hash,name,lang,created_at) VALUES(?,?,?,?,?)",
                            (email, pw_hash, name, lang if lang in ("en", "ar") else "en", _iso(self.now()))).lastrowid
            c.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(?,?,'owner')", (uid, tenant.id))
        token = self._issue_token(uid, "verify", VERIFY_TTL_S)
        self.mailer.send(email, "Verify your DigitalBurj Leads email",
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


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
