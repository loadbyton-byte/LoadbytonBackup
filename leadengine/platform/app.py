"""Platform app: account routes (/auth/*), the workspace dispatcher and the public per-workspace routes.

Every workspace is served by its own `create_app` instance (own SQLite file). The dispatcher is the only thing that decides *which*
instance a request reaches: it resolves the caller (session cookie or workspace API key), applies the role policy and CSRF check on the
path it normalised, and forwards that same normalised path. Nothing in the tenant app ever sees credentials or another workspace."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, parse_qsl, quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from ..api import DASH
from .accounts import SESSION_ABSOLUTE_S, AccountError, AccountService, SessionInfo, csrf_for
from .control import ControlDB
from .mailer import Mailer, make_mailer
from .rbac import allowed, normalize_path
from . import site
from .security import Encryptor
from .tenancy import TenantManager

log = logging.getLogger("leadengine.platform")

SESSION_COOKIE, CSRF_COOKIE, CSRF_HEADER = "dbl_session", "dbl_csrf", "x-csrf-token"
MAX_JSON = 16 * 1024
LIMIT_AUTH, LIMIT_PUBLIC, LIMIT_API, LIMIT_OTHER = 32 * 1024, 256 * 1024, 8 * 1024 * 1024, 64 * 1024     # bytes buffered per request
REGISTER_FLOOR_S, FORGOT_FLOOR_S = 0.4, 0.15      # minimum response time: new and existing addresses look alike
# First path segment of the workspace API (what the dispatcher takes over). Everything else belongs to the platform app.
TENANT_PREFIXES = frozenset("leads agents tasks roi spend inventory viewings viewings.ics radar stats audit ingest connectors tools markets "
                            "maintenance demo overview magnets followups".split())
PUBLIC_EXACT = frozenset({"/capture", "/widget.js", "/optout", "/webhooks/meta", "/webhooks/whatsapp", "/unsubscribe"})
PUBLIC_FORM = re.compile(r"^/webhooks/form/[a-z0-9_-]{1,32}$")
PUBLIC_PAGES = re.compile(r"^/p(/(affordability|costs|yield|mortgage|concierge|lead))?$")        # lead magnets + concierge
EMAIL_OK = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']+\.[^@\s<>\"']{2,}$")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
STATUS = {"rate_limited": 429, "forbidden": 403, "not_verified": 403, "invalid_credentials": 401, "already_member": 409}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


def safe_next(target: str) -> bool:
    """Only same-site absolute paths: never a scheme, host, protocol-relative or backslash form."""
    return (isinstance(target, str) and target.startswith("/") and not target.startswith("//") and "\\" not in target
            and ":" not in target.split("?")[0] and not any(ord(c) < 0x20 for c in target) and len(target) <= 300)


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
    def _hardened(send, no_store: bool = False, head: bool = False, framing: bool = False):
        """`framing=True` (public lead-magnet pages) leaves framing to the page's own CSP `frame-ancestors` instead of sending X-Frame-Options: DENY."""
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
        if not framing:
            extra.append((b"x-frame-options", b"DENY"))
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
        if not tenant or not (rest in PUBLIC_EXACT or PUBLIC_FORM.match(rest) or PUBLIC_PAGES.match(rest)):
            return await self._reply(_not_found(), scope, receive, send)
        try:
            with self.tenants.lease(tenant.id) as tapp:
                fwd = self._forward_scope(scope, rest, {b"cookie", b"x-api-key", b"x-csrf-token"})
                await tapp(fwd, receive, self._hardened(send, framing=bool(PUBLIC_PAGES.match(rest))))
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
                    scoped = await self._scope_agent(fwd, receive, tapp.state.db, info.agent_id, method, path)
                    if isinstance(scoped, JSONResponse):
                        return await self._reply(scoped, scope, receive, send)
                    fwd, receive = scoped
                await tapp(fwd, receive, self._hardened(send, no_store=True, head=head))
        except KeyError:
            await self._reply(_not_found(), scope, receive, send)

    async def _scope_agent(self, fwd: dict, receive, db, agent_id: int | None, method: str, path: str):
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
        row = db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (int(m.group(1)),))
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
    if s.signup_mode not in ("open", "invite"):
        raise ValueError("SIGNUP_MODE must be 'open' or 'invite'")
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

    class AssetFiles(StaticFiles):
        """Brand assets, fonts and CSS only: the HTML dashboards under dashboard/ must not be reachable here (they would skip the strict CSP)."""
        async def get_response(self, path, scope):
            if path.lower().endswith((".html", ".htm")):
                raise HTTPException(404)
            return await super().get_response(path, scope)

    app.mount("/static", AssetFiles(directory=DASH.parent), name="static")

    app.mount("/site-static", StaticFiles(directory=site.SITE_DIR / "static"), name="site-static")
    LANG_COOKIE = "dbl_lang"
    CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"

    def page_lang(request: Request) -> str:
        return site.detect_lang(request.headers.get("accept-language"), request.cookies.get(LANG_COOKIE), request.query_params.get("lang"))

    def html_page(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
        lang = page_lang(request)
        ctx = {"base_url": s.base_url, **(ctx or {})}
        return HTMLResponse(site.render_page(name, lang, ctx), status, headers={"Content-Security-Policy": CSP, "Vary": "Accept-Language, Cookie",
                                                                              "Cache-Control": "no-cache"})

    def contact_block(request: Request) -> str:
        st = site.strings(page_lang(request))
        if s.contact_email and EMAIL_OK.match(s.contact_email):
            e = html.escape(s.contact_email, quote=True)
            return f'<p><strong>{html.escape(st["contact.email_label"])}</strong> <a href="mailto:{e}">{e}</a></p>'
        return f'<p class="mute">{html.escape(st["contact.none"])}</p>'

    def make_page_route(name: str):
        def route(request: Request):
            ctx = {"contact_block": contact_block(request)} if name == "contact" else {}
            return html_page(request, name, ctx)
        return route

    for _name, (_path, _tpl) in site.PAGES.items():
        app.add_api_route(_path, make_page_route(_name), methods=["GET"], response_class=HTMLResponse, include_in_schema=False)

    @app.get("/lang/{lang}", include_in_schema=False)
    def set_lang(lang: str, next: str = "/"):
        if lang not in site.LANGS:
            return _not_found()
        target = next if safe_next(next) else "/"
        resp = RedirectResponse(target, 303)
        resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 86400, samesite="lax", secure=not s.dev_insecure_cookies, path="/")
        return resp

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        return Response(site.sitemap_xml(s.base_url), media_type="application/xml")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /auth/\nSitemap: {s.base_url.rstrip('/')}/sitemap.xml\n")

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

    def set_session_cookies(resp, token: str):
        secure = not s.dev_insecure_cookies
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_ABSOLUTE_S, httponly=True, secure=secure, samesite="lax", path="/")
        resp.set_cookie(CSRF_COOKIE, csrf_for(token), max_age=SESSION_ABSOLUTE_S, httponly=False, secure=secure, samesite="lax", path="/")
        return resp

    def drop_old_session(request: Request) -> None:
        old = session_cookie(request.headers.get("cookie", ""))
        if old:
            accounts.logout(old)                                  # a pre-set session id is never adopted

    def start_session(request: Request, token: str) -> JSONResponse:
        drop_old_session(request)
        return set_session_cookies(_json(200, {"status": "ok"}), token)

    def login_result(request: Request, r) -> JSONResponse:
        if r.status == "ok":
            return start_session(request, r.session_token)
        if r.status == "mfa_required":
            return _json(200, {"status": "mfa_required", "mfa_token": r.mfa_token})
        raise ApiError(401, "invalid_credentials", "Invalid email or password.")

    # ------------------------------------------------------------------ HTML auth pages (work without JavaScript)
    FORM_COOKIE = "dbl_form" if s.dev_insecure_cookies else "__Host-dbl_form"          # __Host- blocks cookie tossing from sibling subdomains
    form_key = hashlib.sha256(b"dbl-form-csrf" + s.encryption_key.encode()).digest()

    def form_token(cookie: str) -> str:
        return hmac.new(form_key, cookie.encode(), hashlib.sha256).hexdigest()[:40]

    def esc(x: str) -> str:
        return html.escape(x, quote=True)

    def field(label: str, name: str, typ: str, value: str = "", hint: str = "", extra: str = "") -> str:
        h = f'<span class="hint">{esc(hint)}</span>' if hint else ""
        return f'<label>{esc(label)}<input name="{name}" type="{typ}" value="{esc(value)}" required {extra}>{h}</label>'

    def build_form(action: str, fields: list[str], submit: str, token: str, hidden: dict | None = None) -> str:
        hid = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in {"_csrf": token, **(hidden or {})}.items())
        return f'<form class="form" method="post" action="{action}">{hid}{"".join(fields)}<button class="btn" type="submit">{esc(submit)}</button></form>'

    def banner(kind: str, text_: str) -> str:
        return f'<p class="{kind}" role="{"alert" if kind == "error" else "status"}">{esc(text_)}</p>' if text_ else ""

    def auth_response(request: Request, name: str, build, status: int = 200, message: str = "", kind: str = "error", extra_cookies=None):
        """Render an auth page with a fresh CSRF-bound form token. `build(token, st)` returns the form HTML (or "" for a message-only page)."""
        lang = page_lang(request)
        st = site.strings(lang)
        cookie = request.cookies.get(FORM_COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", cookie):
            cookie = secrets.token_urlsafe(24)
        path = request.url.path
        nxt = path + ("?" + request.url.query if request.url.query else "")
        ctx = {"base_url": s.base_url, "path": path, "lang_next_q": quote(nxt, safe="") if len(nxt) <= 300 else "", "robots": '<meta name="robots" content="noindex,nofollow">',
               "message": banner(kind, message), "form": build(form_token(cookie), st)}
        resp = HTMLResponse(site.render_page(name, lang, ctx), status, headers={
            "Content-Security-Policy": CSP, "Cache-Control": "no-store", "Vary": "Accept-Language, Cookie"})
        resp.set_cookie(FORM_COOKIE, cookie, max_age=3600, httponly=True, secure=not s.dev_insecure_cookies, samesite="lax", path="/")
        for fn in extra_cookies or []:
            fn(resp)
        return resp

    async def read_form(request: Request) -> dict | None:
        """URL-encoded form fields if the body is a form carrying a valid CSRF token, else None."""
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            return None
        raw = (await request.body()).decode("utf-8", "replace")
        data = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        cookie = request.cookies.get(FORM_COOKIE, "")
        if not cookie or not hmac.compare_digest(data.get("_csrf", "").encode(), form_token(cookie).encode()):
            return None
        return data

    def form_text(d: dict, key: str, limit: int = 300) -> str:
        return d.get(key, "")[:limit]

    def next_param(d: dict, request: Request) -> str:
        n = d.get("next") or request.query_params.get("next", "")
        return n if safe_next(n) else ""

    ERR = {"weak_password": "auth.err.weak", "rate_limited": "auth.err.rate", "invalid_invite": "auth.err.token", "invalid_credentials": "auth.err.login",
           "invalid_code": "auth.err.mfa"}

    def err_text(request: Request, key: str) -> str:
        return site.strings(page_lang(request))[key]

    # --- /start
    def start_form(request: Request, values: dict | None = None):
        v = values or {}

        def build(token, st):
            return build_form("/start", [field(st["auth.email"], "email", "email", v.get("email", ""), extra='autocomplete="email" maxlength="254"'),
                                         field(st["auth.password"], "password", "password", hint=st["auth.pw_hint"], extra='autocomplete="new-password" minlength="12" maxlength="256"'),
                                         field(st["auth.name"], "name", "text", v.get("name", ""), extra='autocomplete="name" maxlength="120"'),
                                         field(st["auth.agency"], "agency_name", "text", v.get("agency_name", ""), extra='autocomplete="organization" maxlength="120"')],
                              st["auth.start.submit"], token)
        return build

    @app.get("/start", include_in_schema=False)
    def start_page(request: Request):
        if request.query_params.get("sent") == "1":
            st = site.strings(page_lang(request))
            return auth_response(request, "auth_start", lambda t, st_: "", message=st["auth.start.done_b"], kind="okmsg")
        return auth_response(request, "auth_start", start_form(request), message=err_text(request, "auth.err.closed") if s.signup_mode == "invite" else "")

    @app.post("/start", include_in_schema=False)
    async def start_submit(request: Request):
        d = await read_form(request)
        if d is None:
            return auth_response(request, "auth_start", start_form(request), 403, err_text(request, "auth.err.csrf"))
        if s.signup_mode == "invite":
            return auth_response(request, "auth_start", start_form(request), 403, err_text(request, "auth.err.closed"))
        t0 = time.perf_counter()
        try:
            await run(accounts.register, form_text(d, "email", 254), form_text(d, "password"), form_text(d, "name", 200), form_text(d, "agency_name", 200),
                      page_lang(request), client_ip(request))
        except AccountError as e:
            await floor(t0, REGISTER_FLOOR_S)
            key = ERR.get(e.code, "auth.err.generic")
            return auth_response(request, "auth_start", start_form(request, {k: d.get(k, "") for k in ("email", "name", "agency_name")}), 400 if e.code != "rate_limited" else 429, err_text(request, key))
        await floor(t0, REGISTER_FLOOR_S)
        return RedirectResponse("/start?sent=1", 303)

    # --- /login (+ MFA)
    def login_form(request: Request, email: str = "", nxt: str = ""):
        def build(token, st):
            return build_form("/login", [field(st["auth.email"], "email", "email", email, extra='autocomplete="username" maxlength="254"'),
                                         field(st["auth.password"], "password", "password", extra='autocomplete="current-password" maxlength="300"')],
                              st["auth.login.submit"], token, {"next": nxt} if nxt else None)
        return build

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        st = site.strings(page_lang(request))
        note = st["auth.reset.done"] if request.query_params.get("reset") == "1" else ""
        return auth_response(request, "auth_login", login_form(request, nxt=next_param({}, request)), message=note, kind="okmsg")

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        d = await read_form(request)
        nxt = next_param(d or {}, request)
        if d is None:
            return auth_response(request, "auth_login", login_form(request, nxt=nxt), 403, err_text(request, "auth.err.csrf"))
        r = await run(accounts.login, form_text(d, "email", 254), form_text(d, "password"), client_ip(request))
        if r.status == "ok":
            drop_old_session(request)
            return set_session_cookies(RedirectResponse(nxt or "/app", 303), r.session_token)
        if r.status == "mfa_required":
            resp = RedirectResponse("/login/mfa" + (f"?next={quote(nxt, safe='')}" if nxt else ""), 303)
            resp.set_cookie("dbl_mfa", r.mfa_token, max_age=300, httponly=True, secure=not s.dev_insecure_cookies, samesite="lax", path="/login")
            return resp
        return auth_response(request, "auth_login", login_form(request, form_text(d, "email", 254), nxt), 401, err_text(request, "auth.err.login"))

    def mfa_form(request: Request, nxt: str):
        def build(token, st):
            return build_form("/login/mfa", [field(st["auth.code"], "code", "text", hint=st["auth.mfa.hint"], extra='autocomplete="one-time-code" inputmode="text" maxlength="40"')],
                              st["auth.mfa.submit"], token, {"next": nxt} if nxt else None)
        return build

    @app.get("/login/mfa", include_in_schema=False)
    def mfa_page(request: Request):
        if not request.cookies.get("dbl_mfa"):
            return RedirectResponse("/login", 303)
        return auth_response(request, "auth_mfa", mfa_form(request, next_param({}, request)))

    @app.post("/login/mfa", include_in_schema=False)
    async def mfa_submit(request: Request):
        d = await read_form(request)
        nxt = next_param(d or {}, request)
        tok = request.cookies.get("dbl_mfa", "")
        if not tok:
            return RedirectResponse("/login", 303)
        if d is None:
            return auth_response(request, "auth_mfa", mfa_form(request, nxt), 403, err_text(request, "auth.err.csrf"))
        r = await run(accounts.login_mfa, tok, form_text(d, "code", 40), client_ip(request))
        if r.status != "ok":
            return auth_response(request, "auth_mfa", mfa_form(request, nxt), 401, err_text(request, "auth.err.mfa"))
        drop_old_session(request)
        resp = set_session_cookies(RedirectResponse(nxt or "/app", 303), r.session_token)
        resp.delete_cookie("dbl_mfa", path="/login")
        return resp

    # --- /forgot, /reset
    def forgot_form(request: Request):
        def build(token, st):
            return build_form("/forgot", [field(st["auth.email"], "email", "email", extra='autocomplete="email" maxlength="254"')], st["auth.forgot.submit"], token)
        return build

    @app.get("/forgot", include_in_schema=False)
    def forgot_page(request: Request):
        if request.query_params.get("sent") == "1":
            return auth_response(request, "auth_forgot", lambda t, st: "", message=err_text(request, "auth.forgot.done"), kind="okmsg")
        return auth_response(request, "auth_forgot", forgot_form(request))

    @app.post("/forgot", include_in_schema=False)
    async def forgot_submit(request: Request):
        d = await read_form(request)
        if d is None:
            return auth_response(request, "auth_forgot", forgot_form(request), 403, err_text(request, "auth.err.csrf"))
        t0 = time.perf_counter()
        await run(accounts.request_reset, form_text(d, "email", 254), client_ip(request))
        await floor(t0, FORGOT_FLOOR_S)
        return RedirectResponse("/forgot?sent=1", 303)

    def token_form(action: str, label_fields, submit_key: str, tok: str):
        def build(token, st):
            return build_form(action, label_fields(st), st[submit_key], token, {"token": tok})
        return build

    def reset_fields(st):
        return [field(st["auth.new_password"], "password", "password", hint=st["auth.pw_hint"], extra='autocomplete="new-password" minlength="12" maxlength="256"')]

    @app.get("/reset", include_in_schema=False)
    def reset_page(request: Request):
        tok = request.query_params.get("token", "")[:200]
        return auth_response(request, "auth_reset", token_form("/reset", reset_fields, "auth.reset.submit", tok))

    @app.post("/reset", include_in_schema=False)
    async def reset_submit(request: Request):
        d = await read_form(request)
        tok = (d or {}).get("token", "")[:200]
        form = token_form("/reset", reset_fields, "auth.reset.submit", tok)
        if d is None:
            return auth_response(request, "auth_reset", form, 403, err_text(request, "auth.err.csrf"))
        try:
            ok = await run(accounts.reset_password, tok, form_text(d, "password"))
        except AccountError as e:
            return auth_response(request, "auth_reset", form, 400, err_text(request, ERR.get(e.code, "auth.err.generic")))
        if not ok:
            return auth_response(request, "auth_reset", lambda t, st: "", 400, err_text(request, "auth.err.token"))
        return RedirectResponse("/login?reset=1", 303)

    # --- /verify (GET only shows a button: mail scanners that prefetch links must not burn the one-time token)
    @app.get("/verify", include_in_schema=False)
    def verify_page(request: Request):
        tok = request.query_params.get("token", "")[:200]
        return auth_response(request, "auth_verify", token_form("/verify", lambda st: [], "auth.verify.h1", tok))

    @app.post("/verify", include_in_schema=False)
    async def verify_submit(request: Request):
        d = await read_form(request)
        if d is None:
            return auth_response(request, "auth_verify", lambda t, st: "", 403, err_text(request, "auth.err.csrf"))
        if await run(accounts.verify_email, form_text(d, "token", 200)):
            return auth_response(request, "auth_verify", lambda t, st: f'<p><a class="btn" href="/login">{esc(st["auth.verify.go"])}</a></p>',
                                 message=err_text(request, "auth.verify.ok"), kind="okmsg")
        return auth_response(request, "auth_verify", lambda t, st: "", 400, err_text(request, "auth.verify.bad"))

    # --- /invite
    def invite_fields(st):
        return [field(st["auth.name"], "name", "text", extra='autocomplete="name" maxlength="120"'),
                field(st["auth.password"], "password", "password", hint=st["auth.invite.hint"], extra='autocomplete="new-password" maxlength="300"')]

    @app.get("/invite", include_in_schema=False)
    def invite_page(request: Request):
        tok = request.query_params.get("token", "")[:200]
        return auth_response(request, "auth_invite", token_form("/invite", invite_fields, "auth.invite.submit", tok))

    @app.post("/invite", include_in_schema=False)
    async def invite_submit(request: Request):
        d = await read_form(request)
        tok = (d or {}).get("token", "")[:200]
        form = token_form("/invite", invite_fields, "auth.invite.submit", tok)
        if d is None:
            return auth_response(request, "auth_invite", form, 403, err_text(request, "auth.err.csrf"))
        try:
            await run(accounts.accept_invite, tok, form_text(d, "password"), form_text(d, "name", 200))
        except AccountError as e:
            bad = e.code == "invalid_invite"
            return auth_response(request, "auth_invite", (lambda t, st: "") if bad else form, 400, err_text(request, ERR.get(e.code, "auth.err.generic")))
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------------ workspace settings, team and the dashboard shell
    PROFILE_KEYS = ("agency_name", "agent_name", "brn", "trakheesi", "whatsapp_phone_id")
    SECRET_KEYS = ("meta_verify_token", "meta_app_secret", "meta_page_token", "whatsapp_token", "form_webhook_secret")
    ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]{1,200}(:\d{1,5})?$")

    def owner_only(info: SessionInfo) -> None:
        if info.role != "owner":
            raise ApiError(403, "forbidden", "Only the workspace owner can do that.")

    @app.get("/auth/settings")
    def get_settings(request: Request):
        info, _ = current(request)
        owner_only(info)
        cfg = tenants.settings_for(info.tenant_id)
        key = control.one("SELECT public_key FROM tenants WHERE id=?", (info.tenant_id,))["public_key"]
        url = f"{s.base_url.rstrip('/')}/t/{key}/widget.js"
        have = tenants.public_settings(info.tenant_id)
        base_pub = f"{s.base_url.rstrip('/')}/t/{key}/p"
        pages = [{"tool": tool, "url": f"{base_pub}/{tool}" if tool else base_pub,
                  "iframe": f'<iframe src="{base_pub}/{tool}" title="{label}" loading="lazy" width="100%" height="640"></iframe>'}
                 for tool, label in (("affordability", "Mortgage affordability calculator"), ("costs", "Property buying costs calculator"),
                                     ("yield", "Rental yield calculator"), ("mortgage", "Mortgage payment calculator"), ("concierge", "Property enquiry assistant"))]
        return _json(200, {**{k: getattr(cfg, k) for k in PROFILE_KEYS}, "capture_origins": list(cfg.capture_origins), "public_pages": pages,
                           "secrets": {k: bool(have.get(k)) for k in SECRET_KEYS},
                           "embed": {"widget_url": url, "snippet": f'<div id="digitalburj-leads"></div>\n<script src="{url}" data-source="website" defer></script>'}})

    @app.put("/auth/settings")
    async def put_settings(request: Request):
        info, _ = current(request, csrf=True)
        owner_only(info)
        d = await body(request)
        allowed_keys = set(PROFILE_KEYS) | set(SECRET_KEYS) | {"capture_origins"}
        if set(d) - allowed_keys:
            raise ApiError(400, "invalid_setting", "Unknown or protected setting: " + ", ".join(sorted(set(d) - allowed_keys)))
        updates: dict = {}
        for k, v in d.items():
            if k == "capture_origins":
                if not isinstance(v, list) or len(v) > 20 or not all(isinstance(o, str) and ORIGIN_RE.match(o) for o in v):
                    raise ApiError(400, "invalid_setting", "capture_origins must be a list of origins like https://example.com")
                updates[k] = [o.lower() for o in v]
            elif not isinstance(v, str) or len(v) > (300 if k in SECRET_KEYS else 120) or any(ord(c) < 32 or ord(c) == 127 for c in v):
                raise ApiError(400, "invalid_setting", f"{k} must be text of at most {300 if k in SECRET_KEYS else 120} characters.")
            elif k in SECRET_KEYS and not v.strip():
                continue                                          # an empty secret field means "leave unchanged"
            elif k == "agency_name" and not v.strip():
                raise ApiError(400, "invalid_setting", "agency_name cannot be empty.")
            else:
                updates[k] = v.strip()
        if not info.email_verified and (updates.get("capture_origins") or any(updates.get(k) for k in SECRET_KEYS)):
            raise ApiError(403, "not_verified", "Verify your email address before adding integrations or website origins.")
        for k, v in updates.items():
            tenants.set_setting(info.tenant_id, k, v)
        if "agency_name" in updates:
            with control.tx() as c:
                c.execute("UPDATE tenants SET name=? WHERE id=?", (updates["agency_name"], info.tenant_id))
        tenants.evict(info.tenant_id)                             # the workspace app is rebuilt with the new settings on its next request
        control.audit("settings_updated", user_id=info.user_id, tenant_id=info.tenant_id, fields=sorted(updates))
        return _json(200, {"ok": True})

    @app.get("/auth/team")
    def team(request: Request):
        info, _ = current(request)
        owner_only(info)
        members = control.q("SELECT u.email, u.name, m.role, m.agent_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.tenant_id=? ORDER BY m.rowid",
                            (info.tenant_id,))
        invites = control.q("SELECT email, role, expires_at FROM invites WHERE tenant_id=? AND accepted_at IS NULL AND expires_at>? ORDER BY expires_at",
                            (info.tenant_id, accounts.now()))
        return _json(200, {"members": [dict(r) for r in members], "invites": [dict(r) for r in invites]})

    app.mount("/app-assets", StaticFiles(directory=DASH.parent / "app"), name="app-assets")
    APP_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

    @app.get("/app", include_in_schema=False)
    def dashboard(request: Request):
        raw = session_cookie(request.headers.get("cookie", ""))
        if not raw or accounts.resolve_session(raw, touch=False) is None:
            return RedirectResponse("/login?next=%2Fapp", 303)
        return HTMLResponse((DASH.parent / "app.html").read_text(encoding="utf-8"), headers={"Content-Security-Policy": APP_CSP, "Cache-Control": "no-store"})

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
                           "workspaces": accounts.workspaces_of(info.user_id),
                           "mfa_enabled": bool(u and u["totp_enabled"])})

    @app.post("/auth/workspace")
    async def switch_workspace(request: Request):
        info, raw = current(request, csrf=True)
        tid = (await body(request)).get("tenant_id")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise ApiError(400, "invalid_input", "tenant_id must be a number.")
        token = await run(accounts.switch_workspace, info.user_id, tid, raw, client_ip(request))
        return set_session_cookies(_json(200, {"status": "ok"}), token)

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
