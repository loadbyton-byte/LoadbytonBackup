"""Public lead-magnet pages: `/p` (index), `/p/<calculator>` and `/p/lead`. Installed on the per-workspace app, so in platform mode they are
reached as `/t/<public key>/p…` and in single-agency mode as `/p…`. Results need no contact details; a lead is created only with explicit consent."""
from __future__ import annotations

import html
import re
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..capture import MAX_BODY, RateLimiter, _clean
from ..identity import normalize_email, normalize_phone
from ..models import Signal
from ..platform import site
from .. import qualify
from . import forms
from .sessions import WebSessions

esc = lambda s: html.escape(str(s), quote=True)                                    # noqa: E731
MAX_PUB_BODY = 8000
CONCIERGE = "concierge"


_ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]{1,200}(:\d{1,5})?", re.ASCII)


def csp(origins: list[str]) -> str:
    ancestors = " ".join(["'self'", *[o for o in origins if isinstance(o, str) and _ORIGIN.fullmatch(o)]])
    return ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; base-uri 'none'; form-action 'self'; "
            f"frame-ancestors {ancestors}")


def _placeholder(v: str) -> bool:
    return not v or v.startswith("<") or v.lower().startswith("your ")


class Pub:
    """Shared helpers for every public page (rendering, headers, counters)."""

    def __init__(self, db, pipe, settings, limiter, client_ip):
        self.db, self.pipe, self.s, self.limiter, self.client_ip = db, pipe, settings, limiter, client_ip
        self.prefix = urlparse(settings.public_url or "").path.rstrip("/")
        m = re.search(r"/t/pk_[\w-]+$", self.prefix)                      # platform mode serves assets from the site root, not under /t/<key>
        self.asset_prefix = self.prefix[:m.start()] if m else self.prefix

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
        full = {"base_url": self.origin(request), "prefix": self.prefix, "asset_prefix": self.asset_prefix, "agency": self.s.agency_name, "agent_line": self.agent_line(lang),
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
        """URL-encoded fields, {} for any other content type, or None when the body exceeds the limit (checked before and while reading)."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_PUB_BODY:
            return None
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_PUB_BODY:
                return None
            chunks.append(chunk)
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            return {}
        return {k: v[-1][:1000] for k, v in parse_qs(b"".join(chunks).decode("utf-8", "replace"), keep_blank_values=True).items()}

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
    checks = RateLimiter(60, 600)              # looser limiter for validation failures; the shared `limiter` is hit only when a lead is stored

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
        if checks.hit(ip):                                         # loose limit on every attempt; the shared limiter counts only stored leads
            return pub.error(request, lang, "pub.err.rate", 429)
        data = await pub.read_form(request)
        if data is None:
            return pub.error(request, lang, "pub.err.toolarge", 413)
        lang = pub.lang(request, data)
        tool = data.get("tool", "")
        if tool not in forms.TOOLS:
            return pub.error(request, lang, "pub.err.notfound", 404)
        if data.get("website"):                                    # honeypot: bots fill hidden fields; look successful, store nothing
            if limiter.hit(ip):
                return pub.error(request, lang, "pub.err.rate", 429)
            return thanks(request, lang)
        inputs, err = forms.parse(tool, data)
        result, err2 = forms.compute(tool, inputs, settings.rates) if not err else ({}, err)
        if err or err2:
            return pub.error(request, lang, err or err2, 422)
        return _store_lead(request, lang, tool, inputs, result, data)

    def _store_lead(request, lang, tool, inputs, result, data):
        st = site.strings(lang)
        name, phone, email = re.sub(r"\s+", " ", _clean(data.get("name"), 100)), normalize_phone(data.get("phone")), normalize_email(data.get("email"))

        def again(key):
            return pub.respond(request, "pub_result", lang, _result_ctx(request, lang, tool, inputs, result, name, data.get("phone", ""), data.get("email", ""),
                                                                        pub.msg(lang, key)), 422, private=True)
        if data.get("consent") != "1":
            return again("pub.err.consent")
        if not (phone or email):
            return again("pub.err.contact")
        text, extra = forms.summary(tool, inputs, result)
        if limiter.hit(client_ip(request)):                        # the shared (/capture) limiter counts stored leads, not mistakes
            return pub.error(request, lang, "pub.err.rate", 429)
        pipe.ingest(Signal(platform="web", external_id=uuid.uuid4().hex, source_type="lead_form", text=text, author_name=name or None,
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

    # ------------------------------------------------------------------ concierge
    sessions = WebSessions(db)
    app.state.web_sessions = sessions

    def question(lang: str, state: dict, retry: bool) -> str:
        st = site.strings(lang)
        step = state["step"]
        text = st["pub.c.q_intent"] if step == "intent" else st["pub.c.q_consent"] if step == "consent" else qualify.Q[lang][step]
        return (qualify.Q[lang]["retry"] if retry else "") + text

    def qa_page(request, lang, token, state, retry=False, status=200):
        st = site.strings(lang)
        step = state["step"]
        form = (f'<form class="form" method="post" action="{pub.prefix}/p/concierge"><input type="hidden" name="lang" value="{lang}">'
                f'<input type="hidden" name="token" value="{esc(token)}"><input type="hidden" name="step" value="{esc(step)}">'
                f'<p class="question" id="q">{esc(question(lang, state, retry))}</p>'
                f'<label for="c-answer">{esc(st["pub.c.answer"])}</label><input id="c-answer" name="answer" type="text" maxlength="300" autocomplete="off" aria-describedby="q" required>'
                f'<p class="mute">{esc(st["pub.c.hint"])}</p><button class="btn" type="submit">{esc(st["pub.c.next"])}</button></form>')
        return pub.respond(request, "pub_concierge", lang, {"title": st["pub.concierge.title"], "description": st["pub.concierge.desc"],
                                                            "canonical_path": "/p/concierge", "form": form}, status, private=True)

    def contact_page(request, lang, token, message="", status=200, values=None):
        st = site.strings(lang)
        v = values or {}
        form = (f'<h2>{esc(st["pub.c.contact_h"])}</h2><form class="form" method="post" action="{pub.prefix}/p/concierge"><input type="hidden" name="lang" value="{lang}">'
                f'<input type="hidden" name="token" value="{esc(token)}"><input type="hidden" name="step" value="contact">'
                f'<div class="hp" aria-hidden="true"><label>Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>'
                f'<div><label for="c-name">{esc(st["pub.lead.name"])}</label><input id="c-name" name="name" type="text" maxlength="100" autocomplete="name" value="{esc(v.get("name", ""))}"></div>'
                f'<div><label for="c-phone">{esc(st["pub.lead.phone"])}</label><input id="c-phone" name="phone" type="tel" maxlength="30" autocomplete="tel" value="{esc(v.get("phone", ""))}"></div>'
                f'<div><label for="c-email">{esc(st["pub.lead.email"])}</label><input id="c-email" name="email" type="email" maxlength="200" autocomplete="email" value="{esc(v.get("email", ""))}"></div>'
                f'<button class="btn" type="submit">{esc(st["pub.lead.submit"])}</button></form>')
        return pub.respond(request, "pub_concierge", lang, {"title": st["pub.concierge.title"], "description": st["pub.concierge.desc"], "canonical_path": "/p/concierge",
                                                            "form": form, "message": message}, status, private=True)

    def restart_page(request, lang, key="pub.err.session"):
        st = site.strings(lang)
        link = f'<p><a class="btn" href="{pub.prefix}/p/concierge?lang={lang}">{esc(st["pub.c.start_over"])}</a></p>'
        return pub.respond(request, "pub_concierge", lang, {"title": st["pub.concierge.title"], "description": st["pub.concierge.desc"], "canonical_path": "/p/concierge",
                                                            "message": pub.msg(lang, key), "form": link}, private=True)

    def declined_page(request, lang):
        st = site.strings(lang)
        return pub.respond(request, "pub_concierge", lang, {"title": st["pub.concierge.title"], "description": st["pub.concierge.desc"], "canonical_path": "/p/concierge",
                                                            "message": f'<p class="okmsg" role="status"><strong>{esc(st["pub.c.declined_h"])}.</strong> {esc(st["pub.c.declined_b"])}</p>',
                                                            "form": f'<p><a class="btn ghost" href="{pub.prefix}/p?lang={lang}">{esc(st["pub.thanks.back"])}</a></p>'}, private=True)

    starts = RateLimiter(10, 600)              # concierge sessions started per IP per 10 minutes

    def concierge_get(request, lang):
        """Rendering the first question creates no server state: refreshes, prefetchers and link previews cost nothing."""
        return qa_page(request, lang, "", {"step": "intent", "answers": {}, "retries": {}, "lang": lang, "phase": "qa"})

    def answers_text(state, lang):
        a = state["answers"]
        days = a.get("timeline")
        # the pipeline's parser reads "within N days/weeks/months" (N <= 99), so write the answer in that form
        when = None if days is None else (f"within {max(1, days)} days" if days <= 99 else f"within {min(99, max(1, round(days / 30)))} months")
        parts = [f"looking to {a['intent']}" if a.get("intent") else "", f"area {a['area']}" if a.get("area") else "",
                 f"budget AED {a['budget']:,.0f}" if a.get("budget") else "", f"timeline {when}" if when else ""]
        extra = {k: v for k, v in (("looking_to", a.get("intent")), ("area", a.get("area")), ("budget", f"AED {a['budget']:.0f}" if a.get("budget") else None),
                                   ("timeline", when)) if v}
        return "Web concierge answers: " + ", ".join(p for p in parts if p) + ".", extra

    async def concierge_post(request, lang):
        data = await pub.read_form(request)
        if data is None:
            return pub.error(request, lang, "pub.err.toolarge", 413)
        lang = pub.lang(request, data)
        token = data.get("token", "")
        answer = _clean(data.get("answer"), 300)
        if token == "" and data.get("step") == "intent":           # first answer: only now does the server create a session
            if qualify.is_stop(answer):
                return declined_page(request, lang)
            if starts.hit(client_ip(request)):
                return pub.error(request, lang, "pub.err.rate", 429)
            token = sessions.start(lang, client_ip(request))
            pub.bump("concierge_start")
        state = sessions.load(token)
        if state is None:
            return restart_page(request, lang)
        if state.get("phase") == "contact":
            return contact_submit(request, lang, token, state, data)
        if qualify.is_stop(answer):                                # STOP works from any step, even a stale tab
            sessions.delete(token)
            pub.bump("concierge_declined")
            return declined_page(request, lang)
        if data.get("step") != state["step"]:                      # a replayed or out-of-order step: show where the chat really is
            return qa_page(request, lang, token, state)
        new, reply, done = qualify.advance(state, answer)
        new["lang"], new["phase"] = lang, "qa"
        if done:
            if new["answers"].get("consent"):
                new["phase"] = "contact"
                new["consent_text"] = f"{state.get('shown', '')} → {answer}"      # exactly what was shown and what the visitor answered
                sessions.save(token, new)
                return contact_page(request, lang, token)
            sessions.delete(token)                                  # "no" (or no answer): nothing is kept
            pub.bump("concierge_declined")
            return declined_page(request, lang)
        retry = reply.startswith(qualify.Q["en"]["retry"]) or reply.startswith(qualify.Q["ar"]["retry"])
        new["shown"] = question(lang, new, retry)
        sessions.save(token, new)
        return qa_page(request, lang, token, new, retry=retry)

    def contact_submit(request, lang, token, state, data):
        st = site.strings(lang)
        if data.get("website"):                                      # honeypot
            sessions.delete(token)
            if limiter.hit(client_ip(request)):
                return pub.error(request, lang, "pub.err.rate", 429)
            return thanks(request, lang)
        name, phone, email = re.sub(r"\s+", " ", _clean(data.get("name"), 100)), normalize_phone(data.get("phone")), normalize_email(data.get("email"))
        if not (phone or email):
            return contact_page(request, lang, token, pub.msg(lang, "pub.err.contact"), 422, {"name": name, "phone": data.get("phone", "")[:30], "email": data.get("email", "")[:200]})
        text, extra = answers_text(state, lang)
        if limiter.hit(client_ip(request)):
            return pub.error(request, lang, "pub.err.rate", 429)
        pipe.ingest(Signal(platform="web", external_id=uuid.uuid4().hex, source_type="lead_form", text=text, author_name=name or None, phone=phone, email=email,
                           consent_marketing=True, consent_text=state.get("consent_text") or st["pub.c.q_consent"],
                           campaign="concierge", extra=extra))
        sessions.delete(token)
        pub.bump("concierge_done")
        return thanks(request, lang)

    return pub
