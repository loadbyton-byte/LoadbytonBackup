"""Platform app: account routes (/auth/*), the workspace dispatcher and the public per-workspace routes.

Every workspace is served by its own `create_app` instance (own SQLite file). The dispatcher is the only thing that decides *which*
instance a request reaches: it resolves the caller (session cookie or workspace API key), applies the role policy and CSRF check on the
path it normalised, and forwards that same normalised path. Nothing in the tenant app ever sees credentials or another workspace."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from .accounts import SESSION_ABSOLUTE_S, AccountError, AccountService, SessionInfo, csrf_for
from .control import ControlDB
from .mailer import Mailer, make_mailer
from .rbac import allowed, normalize_path
from .security import Encryptor
from .tenancy import TenantManager

log = logging.getLogger("leadengine.platform")

SESSION_COOKIE, CSRF_COOKIE, CSRF_HEADER = "dbl_session", "dbl_csrf", "x-csrf-token"
MAX_JSON = 16 * 1024
LIMIT_AUTH, LIMIT_PUBLIC, LIMIT_API, LIMIT_OTHER = 32 * 1024, 256 * 1024, 8 * 1024 * 1024, 64 * 1024     # bytes buffered per request
REGISTER_FLOOR_S, FORGOT_FLOOR_S = 0.4, 0.15      # minimum response time: new and existing addresses look alike
# First path segment of the workspace API (what the dispatcher takes over). Everything else belongs to the platform app.
TENANT_PREFIXES = frozenset("leads agents tasks roi spend inventory viewings viewings.ics radar stats audit ingest connectors tools markets "
                            "maintenance demo overview".split())
PUBLIC_EXACT = frozenset({"/capture", "/widget.js", "/optout", "/webhooks/meta", "/webhooks/whatsapp"})
PUBLIC_FORM = re.compile(r"^/webhooks/form/[a-z0-9_-]{1,32}$")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
STATUS = {"rate_limited": 429, "forbidden": 403, "not_verified": 403, "invalid_credentials": 401, "already_member": 409}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


def session_cookie(header: str) -> str | None:
    """The one session cookie, or None if absent or duplicated (a duplicate could be planted from a sibling origin)."""
    vals = [v for n, _, v in (p.strip().partition("=") for p in header.split(";")) if n == SESSION_COOKIE]
    return vals[0] if len(vals) == 1 and vals[0] else None


async def _read_body(receive, limit: int):
    """Buffer the request body up to `limit`. Returns bytes, or None when the limit is exceeded or the client went away."""
    chunks, total = [], 0
    while True:
        m = await receive()
        if m["type"] != "http.request":
            return None
        total += len(m.get("body", b""))
        if total > limit:
            return None
        chunks.append(m.get("body", b""))
        if not m.get("more_body"):
            return b"".join(chunks)


def _replay(body: bytes, receive):
    sent = False

    async def r():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return r


def _json(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status, headers={"Cache-Control": "no-store"})


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not found"}, 404, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- dispatcher (pure ASGI)
class Dispatcher:
    """Wraps the platform FastAPI app. Takes over the workspace API and the public /t/{key}/... routes; passes the rest through."""

    def __init__(self, app, owner: "FastAPI"):
        self.app, self.owner = app, owner

    @property
    def tenants(self) -> TenantManager:
        return self.owner.state.tenants

    @property
    def accounts(self) -> AccountService:
        return self.owner.state.accounts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = scope.get("raw_path")
        raw_path = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else scope["path"]
        first = raw_path.split("/", 2)[1] if raw_path.startswith("/") and len(raw_path) > 1 else ""
        limit = LIMIT_PUBLIC if first == "t" else LIMIT_API if first in TENANT_PREFIXES else LIMIT_AUTH if raw_path.startswith("/auth/") else LIMIT_OTHER
        if scope["method"].upper() not in SAFE_METHODS:
            declared = dict(scope["headers"]).get(b"content-length", b"")
            body = None if declared.isdigit() and int(declared) > limit else await _read_body(receive, limit)
            if body is None:
                return await self._reply(_json(413, {"error": "too_large", "message": "Request body too large."}), scope, receive, send)
            receive = _replay(body, receive)
        if first == "t":
            return await self._public(scope, receive, send, raw_path)
        if first in TENANT_PREFIXES:
            return await self._tenant_api(scope, receive, send, raw_path)
        await self.app(scope, receive, self._hardened(send, no_store=raw_path.startswith("/auth/")))

    # -- helpers
    @staticmethod
    def _hardened(send, no_store: bool = False, head: bool = False):
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"DENY")]
        if no_store:
            extra.append((b"cache-control", b"no-store"))
        names = {k for k, _ in extra}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": headers + extra}
            elif head and message["type"] == "http.response.body":
                message = {**message, "body": b"", "more_body": message.get("more_body", False)}
            await send(message)
        return wrapped

    async def _reply(self, resp, scope, receive, send):
        await resp(scope, receive, self._hardened(send))

    @staticmethod
    def _forward_scope(scope, path: str, drop: set[bytes], method: str | None = None) -> dict:
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in drop]
        return {**scope, "path": path, "raw_path": path.encode(), "root_path": "", "headers": headers, **({"method": method} if method else {})}

    # -- public, per-workspace routes
    async def _public(self, scope, receive, send, raw_path: str):
        path = normalize_path(raw_path)
        m = re.match(r"^/t/([A-Za-z0-9_-]{1,64})(/.*)$", path or "")
        tenant = self.tenants.tenant_by_key(m.group(1)) if m else None
        rest = m.group(2) if m else ""
        if not tenant or not (rest in PUBLIC_EXACT or PUBLIC_FORM.match(rest)):
            return await self._reply(_not_found(), scope, receive, send)
        try:
            with self.tenants.lease(tenant.id) as tapp:
                fwd = self._forward_scope(scope, rest, {b"cookie", b"x-api-key", b"x-csrf-token"})
                await tapp(fwd, receive, self._hardened(send))
        except KeyError:
            await self._reply(_not_found(), scope, receive, send)

    # -- authenticated workspace API
    async def _tenant_api(self, scope, receive, send, raw_path: str):
        path = normalize_path(raw_path)
        if path is None:
            return await self._reply(_json(400, {"error": "bad_path", "message": "Invalid path."}), scope, receive, send)
        method = scope["method"].upper()
        headers = {k.lower(): v for k, v in scope["headers"]}
        api_key = headers.get(b"x-api-key", b"").decode("latin-1").strip()
        cookie_auth = False
        if api_key:                                            # an API key is authoritative: a bad key never falls back to a session
            tid = self.accounts.verify_api_key(api_key)
            if tid is None:
                return await self._reply(_json(401, {"error": "unauthorized", "message": "Invalid API key."}), scope, receive, send)
            role, info = "manager", None
        else:
            info = self._session(headers)
            if info is None:
                return await self._reply(_json(401, {"error": "unauthorized", "message": "Sign in required."}), scope, receive, send)
            tid, role, cookie_auth = info.tenant_id, info.role, True
        if not allowed(role, method, path):
            return await self._reply(_json(403, {"error": "forbidden", "message": "Your role cannot do that."}), scope, receive, send)
        if cookie_auth and method not in SAFE_METHODS:
            sent = headers.get(CSRF_HEADER.encode(), b"").decode("latin-1")
            if not sent or not hmac.compare_digest(sent.encode(), info.csrf_token.encode()):
                return await self._reply(_json(403, {"error": "csrf", "message": "Missing or invalid CSRF token."}), scope, receive, send)
        head = method == "HEAD"
        try:
            with self.tenants.lease(tid) as tapp:
                fwd = self._forward_scope(scope, path, {b"cookie", b"x-api-key", b"x-csrf-token"}, "GET" if head else None)
                if role == "agent":
                    scoped = await self._scope_agent(fwd, receive, tid, info.agent_id, method, path)
                    if isinstance(scoped, JSONResponse):
                        return await self._reply(scoped, scope, receive, send)
                    fwd, receive = scoped
                await tapp(fwd, receive, self._hardened(send, no_store=True, head=head))
        except KeyError:
            await self._reply(_not_found(), scope, receive, send)

    async def _scope_agent(self, fwd: dict, receive, tid: int, agent_id: int | None, method: str, path: str):
        """Row-level scoping for the agent role: own leads only, own calendar, own tasks. Client-supplied agent filters never survive."""
        me = agent_id if agent_id is not None else -1                       # -1 matches no agent: an unlinked account sees nothing
        qs = [(k, v) for k, v in parse_qsl(fwd.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
              if k not in ("agent_id", "assigned_agent_id")]
        if path == "/leads":
            qs.append(("assigned_agent_id", str(me)))
        elif path in ("/viewings", "/tasks/today"):
            qs.append(("agent_id", str(me)))
        fwd = {**fwd, "query_string": urlencode(qs).encode()}
        m = re.match(r"^/leads/(\d+)(/.*)?$", path)
        if not m:
            return fwd, receive
        row = self.tenants.db_for(tid).one("SELECT assigned_agent_id FROM leads WHERE id=?", (int(m.group(1)),))
        if not row or agent_id is None or row["assigned_agent_id"] != agent_id:
            return _json(403, {"error": "forbidden", "message": "That lead is not assigned to you."})
        sub = m.group(2) or ""
        if method == "POST" and sub in ("/status", "/viewings"):
            body = await _read_body(receive, LIMIT_OTHER)
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return _json(400, {"error": "invalid_json", "message": "Invalid JSON."})
            data.pop("assigned_to", None)                                   # agents cannot reassign
            if sub == "/viewings":
                data["agent_id"] = agent_id                                 # ...or book someone else's calendar
            receive = _replay(json.dumps(data).encode(), receive)
            fwd = {**fwd, "headers": [(k, v) for k, v in fwd["headers"] if k.lower() != b"content-length"]}
        return fwd, receive

    def _session(self, headers: dict) -> SessionInfo | None:
        raw = session_cookie(headers.get(b"cookie", b"").decode("latin-1"))
        return self.accounts.resolve_session(raw) if raw else None


# ---------------------------------------------------------------------------- factory
def create_platform_app(settings: Settings, mailer: Mailer | None = None, inline_mail: bool = False):
    if not settings.control_db:
        raise ValueError("CONTROL_DB is required for platform mode")
    if not settings.encryption_key:
        raise ValueError("ENCRYPTION_KEY is required for platform mode (generate one with leadengine.platform.security.generate_key())")
    s = settings
    control = ControlDB(s.control_db)
    enc = Encryptor(s.encryption_key)
    tenants = TenantManager(control, s.data_dir, s, enc)
    pool = None if inline_mail else ThreadPoolExecutor(max_workers=2, thread_name_prefix="mail")

    def background(fn):
        def run():
            try:
                fn()
            except Exception:
                log.exception("mail send failed")
        pool.submit(run)

    accounts = AccountService(control, mailer or make_mailer(s), tenants, s, encryptor=enc, background=None if inline_mail else background)

    app = FastAPI(title="DigitalBurj Leads", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.control, app.state.tenants, app.state.accounts = s, control, tenants, accounts

    from ..api import DASH
    app.mount("/static", StaticFiles(directory=DASH.parent), name="static")

    @app.exception_handler(ApiError)
    async def _api_error(_, e: ApiError):
        return _json(e.status, {"error": e.code, "message": e.message})

    @app.exception_handler(AccountError)
    async def _account_error(_, e: AccountError):
        return _json(STATUS.get(e.code, 400), {"error": e.code, "message": e.message})

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "?"
        n = s.trusted_proxies
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        return hops[-n] if n > 0 and len(hops) >= n else peer

    async def body(request: Request) -> dict:
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise ApiError(415, "unsupported_media_type", "Send application/json.")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_JSON:
            raise ApiError(413, "too_large", "Request body too large.")
        raw = await request.body()
        if len(raw) > MAX_JSON:
            raise ApiError(413, "too_large", "Request body too large.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "invalid_json", "Invalid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Expected a JSON object.")
        return data

    async def run(fn, *a):
        return await run_in_threadpool(fn, *a)                  # scrypt / SQLite work never blocks the event loop

    async def floor(start: float, seconds: float) -> None:
        rest = seconds * (1 + random.random() * 0.2) - (time.perf_counter() - start)
        if rest > 0:
            await asyncio.sleep(rest)

    def text(d: dict, key: str, limit: int = 300) -> str:
        v = d.get(key, "")
        return v[:limit] if isinstance(v, str) else ""

    def current(request: Request, csrf: bool = False) -> tuple[SessionInfo, str]:
        raw = session_cookie(request.headers.get("cookie", "")) or ""
        info = accounts.resolve_session(raw)
        if info is None:
            raise ApiError(401, "unauthorized", "Sign in required.")
        if csrf and not hmac.compare_digest(request.headers.get(CSRF_HEADER, "").encode(), csrf_for(raw).encode()):
            raise ApiError(403, "csrf", "Missing or invalid CSRF token.")
        return info, raw

    def start_session(request: Request, token: str) -> JSONResponse:
        old = session_cookie(request.headers.get("cookie", ""))
        if old:
            accounts.logout(old)                                  # a pre-set session id is never adopted
        secure = not s.dev_insecure_cookies
        resp = _json(200, {"status": "ok"})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_ABSOLUTE_S, httponly=True, secure=secure, samesite="lax", path="/")
        resp.set_cookie(CSRF_COOKIE, csrf_for(token), max_age=SESSION_ABSOLUTE_S, httponly=False, secure=secure, samesite="lax", path="/")
        return resp

    def login_result(request: Request, r) -> JSONResponse:
        if r.status == "ok":
            return start_session(request, r.session_token)
        if r.status == "mfa_required":
            return _json(200, {"status": "mfa_required", "mfa_token": r.mfa_token})
        raise ApiError(401, "invalid_credentials", "Invalid email or password.")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.post("/auth/register")
    async def register(request: Request):
        if s.signup_mode == "invite":
            raise ApiError(403, "signup_closed", "Sign-up is by invitation only.")
        d = await body(request)
        t0 = time.perf_counter()
        try:
            await run(accounts.register, text(d, "email", 254), text(d, "password", 300), text(d, "name", 200), text(d, "agency_name", 200),
                      text(d, "lang", 5) or "en", client_ip(request))
        finally:
            await floor(t0, REGISTER_FLOOR_S)
        return _json(200, {"ok": True})                           # identical for new and existing addresses

    @app.post("/auth/verify")
    async def verify(request: Request):
        if not accounts.verify_email(text(await body(request), "token", 200)):
            raise ApiError(400, "invalid_token", "This link is not valid or has expired.")
        return _json(200, {"ok": True})

    @app.post("/auth/login")
    async def login(request: Request):
        d = await body(request)
        return login_result(request, await run(accounts.login, text(d, "email", 254), text(d, "password", 300), client_ip(request)))

    @app.post("/auth/login/mfa")
    async def login_mfa(request: Request):
        d = await body(request)
        return login_result(request, await run(accounts.login_mfa, text(d, "mfa_token", 200), text(d, "code", 40), client_ip(request)))

    @app.post("/auth/logout")
    async def logout(request: Request):
        _, raw = current(request, csrf=True)
        accounts.logout(raw)
        resp = _json(200, {"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(CSRF_COOKIE, path="/")
        return resp

    @app.post("/auth/forgot")
    async def forgot(request: Request):
        d = await body(request)
        t0 = time.perf_counter()
        await run(accounts.request_reset, text(d, "email", 254), client_ip(request))
        await floor(t0, FORGOT_FLOOR_S)
        return _json(200, {"ok": True})

    @app.post("/auth/reset")
    async def reset(request: Request):
        d = await body(request)
        if not await run(accounts.reset_password, text(d, "token", 200), text(d, "password", 300)):
            raise ApiError(400, "invalid_token", "This link is not valid or has expired.")
        return _json(200, {"ok": True})

    @app.get("/auth/me")
    def me(request: Request):
        info, _ = current(request)
        t = control.one("SELECT public_key FROM tenants WHERE id=?", (info.tenant_id,))
        u = control.one("SELECT totp_enabled FROM users WHERE id=?", (info.user_id,))
        return _json(200, {"user": {"email": info.email, "name": info.name, "lang": info.lang, "email_verified": info.email_verified},
                           "role": info.role, "agent_id": info.agent_id, "csrf_token": info.csrf_token,
                           "workspace": {"id": info.tenant_id, "name": info.tenant_name, "public_key": t["public_key"] if t else None},
                           "mfa_enabled": bool(u and u["totp_enabled"])})

    @app.post("/auth/password")
    async def password(request: Request):
        info, raw = current(request, csrf=True)
        d = await body(request)
        if not await run(lambda: accounts.change_password(info.user_id, text(d, "old_password", 300), text(d, "new_password", 300), keep_session=raw)):
            raise ApiError(400, "invalid_credentials", "Your current password is not correct.")
        return _json(200, {"ok": True})

    @app.post("/auth/mfa/begin")
    async def mfa_begin(request: Request):
        info, _ = current(request, csrf=True)
        setup = accounts.mfa_begin(info.user_id)
        return _json(200, {"secret": setup.secret, "uri": setup.uri})

    @app.post("/auth/mfa/confirm")
    async def mfa_confirm(request: Request):
        info, _ = current(request, csrf=True)
        return _json(200, {"recovery_codes": accounts.mfa_confirm(info.user_id, text(await body(request), "code", 40))})

    @app.post("/auth/mfa/disable")
    async def mfa_disable(request: Request):
        info, _ = current(request, csrf=True)
        if not await run(accounts.mfa_disable, info.user_id, text(await body(request), "password", 300)):
            raise ApiError(400, "invalid_credentials", "Your password is not correct.")
        return _json(200, {"ok": True})

    @app.post("/auth/invite")
    async def invite(request: Request):
        info, _ = current(request, csrf=True)
        d = await body(request)
        agent_id = d.get("agent_id")
        accounts.invite(info.tenant_id, info.user_id, text(d, "email", 254), text(d, "role", 20),
                        agent_id if isinstance(agent_id, int) and not isinstance(agent_id, bool) else None)
        return _json(200, {"ok": True})

    @app.post("/auth/invite/accept")
    async def invite_accept(request: Request):
        d = await body(request)
        await run(accounts.accept_invite, text(d, "token", 200), text(d, "password", 300), text(d, "name", 200))
        return _json(200, {"ok": True})

    @app.post("/auth/api-key")
    async def api_key(request: Request):
        info, _ = current(request, csrf=True)
        return _json(200, {"api_key": accounts.rotate_api_key(info.tenant_id, info.user_id)})

    app.add_middleware(Dispatcher, owner=app)
    return app
