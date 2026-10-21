"""Public lead-magnet pages: `/p` (index), `/p/<calculator>` and `/p/lead`. Installed on the per-workspace app, so in platform mode they are
reached as `/t/<public key>/p…` and in single-agency mode as `/p…`. Results need no contact details; a lead is created only with explicit consent."""
from __future__ import annotations

import html
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..capture import MAX_BODY
from ..identity import normalize_email, normalize_phone
from ..models import Signal
from ..platform import site
from . import forms

esc = lambda s: html.escape(str(s), quote=True)                                    # noqa: E731
MAX_PUB_BODY = 8000
CONCIERGE = "concierge"


def csp(origins: list[str]) -> str:
    ancestors = " ".join(["'self'", *[o for o in origins if o.startswith(("http://", "https://")) and not any(c in o for c in " ;'\"\r\n")]])
    return ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; base-uri 'none'; form-action 'self'; "
            f"frame-ancestors {ancestors}")


def _placeholder(v: str) -> bool:
    return not v or v.startswith("<") or v.lower().startswith("your ")


class Pub:
    """Shared helpers for every public page (rendering, headers, counters)."""

    def __init__(self, db, pipe, settings, limiter, client_ip):
        self.db, self.pipe, self.s, self.limiter, self.client_ip = db, pipe, settings, limiter, client_ip
        self.prefix = urlparse(settings.public_url or "").path.rstrip("/")

    def lang(self, request: Request, form: dict | None = None) -> str:
        q = (form or {}).get("lang") or request.query_params.get("lang")
        return site.detect_lang(request.headers.get("accept-language"), None, q)

    def origin(self, request: Request) -> str:
        u = urlparse(self.s.public_url or "")
        return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else str(request.base_url).rstrip("/")

    def agent_line(self, lang: str) -> str:
        st = site.strings(lang)
        bits = [f"{esc(st[k])}: {esc(v)}" for k, v in (("pub.agent", self.s.agent_name), ("pub.brn", self.s.brn), ("pub.permit", self.s.trakheesi)) if not _placeholder(v)]
        return " · ".join(bits)

    def respond(self, request: Request, template: str, lang: str, ctx: dict, status: int = 200, private: bool = False) -> HTMLResponse:
        st = site.strings(lang)
        full = {"base_url": self.origin(request), "prefix": self.prefix, "agency": self.s.agency_name, "agent_line": self.agent_line(lang),
                "message": "", **ctx}
        if private:
            full["robots"] = '<meta name="robots" content="noindex,nofollow">'
        headers = {"Content-Security-Policy": csp(self.s.capture_origins), "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                   "Cache-Control": "no-store" if private else "no-cache", "Vary": "Accept-Language"}
        return HTMLResponse(site.render_with_base(template, "pub_base", lang, full), status, headers=headers)

    def error(self, request: Request, lang: str, key: str, status: int) -> HTMLResponse:
        st = site.strings(lang)
        return self.respond(request, "pub_error", lang, {"title": st["pub.err.title"], "description": st["pub.err.desc"], "canonical_path": "/p",
                                                         "message": f'<p class="error" role="alert">{esc(st[key])}</p>'}, status, private=True)

    def bump(self, key: str) -> None:
        try:
            self.db.bump(key)
        except Exception:                                    # counters must never break a page
            pass

    async def read_form(self, request: Request) -> dict | None:
        raw = await request.body()
        if len(raw) > MAX_PUB_BODY:
            return None
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            return {}
        return {k: v[-1][:1000] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

    # ---- html pieces (every dynamic value escaped)
    def field(self, lang: str, name: str, value: str = "") -> str:
        st = site.strings(lang)
        inputmode = "decimal"
        return (f'<label for="f-{name}">{esc(st["pub.f." + name])}</label><input id="f-{name}" name="{name}" type="text" inputmode="{inputmode}" '
                f'autocomplete="off" maxlength="24" value="{esc(value)}">')

    def calc_form(self, lang: str, tool: str, values: dict) -> str:
        st = site.strings(lang)
        fields = "".join(f'<div>{self.field(lang, f, values.get(f, ""))}</div>' for f in forms.FIELDS[tool])
        return (f'<form class="form" method="post" action="{self.prefix}/p/{tool}"><input type="hidden" name="lang" value="{lang}">{fields}'
                f'<button class="btn" type="submit">{esc(st["pub.calculate"])}</button></form>')

    def results_html(self, lang: str, tool: str, result: dict) -> str:
        st = site.strings(lang)
        rows = "".join(f'<dt>{esc(st["pub.r." + k])}</dt><dd>{esc(forms.fmt(k, result[k]))}</dd>' for k in forms.ROWS[tool])
        note = ""
        if tool == "affordability":
            note = f'<p class="mute">{esc(st["pub.r.limited_" + result["limited_by"]])}</p>'
        return f'<dl class="kv">{rows}</dl>{note}'

    def lead_form(self, lang: str, tool: str, inputs: dict, name: str = "", phone: str = "", email: str = "") -> str:
        st = site.strings(lang)
        hidden = "".join(f'<input type="hidden" name="{f}" value="{esc(forms.raw(inputs[f]))}">' for f in forms.FIELDS[tool])
        return (f'<form class="form" method="post" action="{self.prefix}/p/lead"><input type="hidden" name="lang" value="{lang}"><input type="hidden" name="tool" value="{tool}">{hidden}'
                f'<div class="hp" aria-hidden="true"><label>Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>'
                f'<div><label for="l-name">{esc(st["pub.lead.name"])}</label><input id="l-name" name="name" type="text" maxlength="100" autocomplete="name" value="{esc(name)}"></div>'
                f'<div><label for="l-phone">{esc(st["pub.lead.phone"])}</label><input id="l-phone" name="phone" type="tel" maxlength="30" autocomplete="tel" value="{esc(phone)}"></div>'
                f'<div><label for="l-email">{esc(st["pub.lead.email"])}</label><input id="l-email" name="email" type="email" maxlength="200" autocomplete="email" value="{esc(email)}"></div>'
                f'<label class="check"><input type="checkbox" name="consent" value="1" required><span>{esc(st["pub.consent"])}</span></label>'
                f'<button class="btn" type="submit">{esc(st["pub.lead.submit"])}</button></form>')

    def msg(self, lang: str, key: str, kind: str = "error") -> str:
        return f'<p class="{kind}" role="{"alert" if kind == "error" else "status"}">{esc(site.strings(lang)[key])}</p>'


def install(app: FastAPI, *, db, pipe, settings, limiter, client_ip) -> Pub:
    pub = Pub(db, pipe, settings, limiter, client_ip)
    app.state.pub = pub

    @app.get("/p", include_in_schema=False)
    def index(request: Request):
        lang = pub.lang(request)
        st = site.strings(lang)
        cards = "".join(f'<article class="card"><a class="tool" href="{pub.prefix}/p/{t}">{esc(st["pub." + t + ".title"])}</a><p>{esc(st["pub." + t + ".card"])}</p></article>'
                        for t in (*forms.TOOLS, CONCIERGE))
        return pub.respond(request, "pub_index", lang, {"title": st["pub.index.title"], "description": st["pub.index.desc"], "canonical_path": "/p", "tools": cards})

    @app.post("/p/lead", include_in_schema=False)
    async def lead(request: Request):
        lang = pub.lang(request)
        ip = client_ip(request)
        if limiter.hit(ip):
            return pub.error(request, lang, "pub.err.rate", 429)
        data = await pub.read_form(request)
        if data is None:
            return pub.error(request, lang, "pub.err.toolarge", 413)
        lang = pub.lang(request, data)
        tool = data.get("tool", "")
        if tool not in forms.TOOLS:
            return pub.error(request, lang, "pub.err.notfound", 404)
        if data.get("website"):                                    # honeypot: bots fill hidden fields; look successful, store nothing
            return thanks(request, lang)
        inputs, err = forms.parse(tool, data)
        result, err2 = forms.compute(tool, inputs, settings.rates) if not err else ({}, err)
        if err or err2:
            return pub.error(request, lang, err or err2, 422)
        return _store_lead(request, lang, tool, inputs, result, data)

    def _store_lead(request, lang, tool, inputs, result, data):
        st = site.strings(lang)
        name, phone, email = data.get("name", "").strip()[:100], normalize_phone(data.get("phone")), normalize_email(data.get("email"))

        def again(key):
            return pub.respond(request, "pub_result", lang, _result_ctx(request, lang, tool, inputs, result, name, data.get("phone", ""), data.get("email", ""),
                                                                        pub.msg(lang, key)), 422, private=True)
        if data.get("consent") != "1":
            return again("pub.err.consent")
        if not (phone or email):
            return again("pub.err.contact")
        text, extra = forms.summary(tool, inputs, result)
        res = pipe.ingest(Signal(platform="web", external_id=uuid.uuid4().hex, source_type="lead_form", text=text, author_name=name or None,
                                 phone=phone, email=email, consent_marketing=True, consent_text=st["pub.consent"],
                                 campaign=f"lead-magnet:{tool}", extra=extra))
        pub.bump(f"magnet_{tool}_lead")
        return thanks(request, lang)

    def thanks(request, lang):
        st = site.strings(lang)
        return pub.respond(request, "pub_thanks", lang, {"title": st["pub.thanks.title"], "description": st["pub.thanks.desc"], "canonical_path": "/p"}, private=True)

    def _result_ctx(request, lang, tool, inputs, result, name="", phone="", email="", message=""):
        st = site.strings(lang)
        return {"title": st[f"pub.{tool}.title"], "description": st[f"pub.{tool}.desc"], "canonical_path": f"/p/{tool}", "h1": st[f"pub.{tool}.h1"],
                "results": pub.results_html(lang, tool, result), "lead_form": pub.lead_form(lang, tool, inputs, name, phone, email),
                "again_href": f"{pub.prefix}/p/{tool}?lang={lang}", "message": message}

    @app.get("/p/{tool}", include_in_schema=False)
    def calc_page(tool: str, request: Request):
        lang = pub.lang(request)
        if tool == CONCIERGE:
            return concierge_get(request, lang)
        if tool not in forms.TOOLS:
            return pub.error(request, lang, "pub.err.notfound", 404)
        pub.bump(f"magnet_{tool}_view")
        st = site.strings(lang)
        return pub.respond(request, "pub_calc", lang, {"title": st[f"pub.{tool}.title"], "description": st[f"pub.{tool}.desc"], "canonical_path": f"/p/{tool}",
                                                       "h1": st[f"pub.{tool}.h1"], "lead": st[f"pub.{tool}.lead"], "form": pub.calc_form(lang, tool, {})})

    @app.post("/p/{tool}", include_in_schema=False)
    async def calc_submit(tool: str, request: Request):
        lang = pub.lang(request)
        if tool == CONCIERGE:
            return await concierge_post(request, lang)
        if tool not in forms.TOOLS:
            return pub.error(request, lang, "pub.err.notfound", 404)
        data = await pub.read_form(request)
        if data is None:
            return pub.error(request, lang, "pub.err.toolarge", 413)
        lang = pub.lang(request, data)
        st = site.strings(lang)
        inputs, err = forms.parse(tool, data)
        result, err2 = forms.compute(tool, inputs, settings.rates) if not err else ({}, err)
        if err or err2:
            values = {f: data.get(f, "")[:24] for f in forms.FIELDS[tool]}
            return pub.respond(request, "pub_calc", lang, {"title": st[f"pub.{tool}.title"], "description": st[f"pub.{tool}.desc"], "canonical_path": f"/p/{tool}",
                                                           "h1": st[f"pub.{tool}.h1"], "lead": st[f"pub.{tool}.lead"], "form": pub.calc_form(lang, tool, values),
                                                           "message": pub.msg(lang, err or err2)}, 422, private=True)
        pub.bump(f"magnet_{tool}_submit")
        return pub.respond(request, "pub_result", lang, _result_ctx(request, lang, tool, inputs, result), private=True)

    # concierge is implemented in a later task; until then these routes answer "not found"
    def concierge_get(request, lang):
        return pub.error(request, lang, "pub.err.notfound", 404)

    async def concierge_post(request, lang):
        return pub.error(request, lang, "pub.err.notfound", 404)

    return pub
