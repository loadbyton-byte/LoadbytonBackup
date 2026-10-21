"""Public lead capture (embeddable widget -> POST /capture). Same defences as the Digital Burj site's api/lead.js:
origin check, honeypot, per-IP rate limit; plus an explicit consent checkbox whose exact wording is stored as evidence."""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import urlparse

from .config import Settings
from .identity import normalize_email, normalize_phone
from .models import Signal

CONSENT_TEXT = ("I agree to be contacted about this enquiry and similar properties by phone, WhatsApp or email. "
                "I can opt out at any time.")
MAX_BODY = 8000
MAX_KEYS = 5000
FIELD_CAPS = {"name": 100, "phone": 30, "email": 200, "message": 1000}
EXTRA_FIELDS = {"looking_to": 40, "area": 80, "budget": 40, "bedrooms": 10, "country": 60, "city": 80}   # allow-listed qualification answers
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RateLimiter:
    def __init__(self, n: int, window_s: int):
        self.n, self.window = n, window_s
        self._hits: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record a request; True means the caller is over the limit."""
        now = time.monotonic() if now is None else now
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        q.append(now)
        if len(self._hits) > MAX_KEYS:
            self._prune(now)
        return len(q) > self.n

    def _prune(self, now: float) -> None:
        """Drop expired keys; if still over capacity drop one-hit keys. Repeat offenders are never forgotten wholesale."""
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
            del self._hits[k]
        if len(self._hits) > MAX_KEYS:
            for k in [k for k, q in self._hits.items() if len(q) <= 1]:
                del self._hits[k]


def origin_allowed(origin: str | None, allowed: list[str], host: str | None) -> bool:
    if origin is None:                    # non-browser client (server-to-server); still rate-limited and consent-gated
        return True
    if origin in allowed:
        return True
    return not allowed and bool(host) and urlparse(origin).hostname == host.split(":")[0]


def _clean(v, cap: int) -> str:
    return _CONTROL.sub("", "" if v is None else str(v)).strip()[:cap]


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "on", "yes")


def handle_capture(pipe, settings: Settings, payload, origin: str | None, ip: str, limiter: RateLimiter,
                   host: str | None = None) -> tuple[int, dict]:
    if not origin_allowed(origin, settings.capture_origins, host):
        return 403, {"error": "origin not allowed"}
    if limiter.hit(ip):
        return 429, {"error": "Too many requests. Please try again shortly."}
    if not isinstance(payload, dict):
        return 400, {"error": "invalid payload"}
    if len(json.dumps(payload, default=str)) > MAX_BODY:
        return 413, {"error": "payload too large"}
    if payload.get("website"):            # honeypot: bots fill hidden fields; pretend success, store nothing
        return 200, {"ok": True}
    if not _truthy(payload.get("consent")):
        return 400, {"error": "consent is required"}
    f = {k: _clean(payload.get(k), cap) for k, cap in FIELD_CAPS.items()}
    phone, email = normalize_phone(f["phone"]), normalize_email(f["email"])
    if not (phone or email):
        return 400, {"error": "a valid phone number or email is required"}
    extra = {k: v for k, cap in EXTRA_FIELDS.items() if (v := _clean(payload.get(k), cap))}
    pipe.ingest(Signal(platform="web", external_id=uuid.uuid4().hex, source_type="lead_form", text=f["message"],
                       author_name=f["name"] or None, phone=phone, email=email, consent_marketing=True,
                       consent_text=CONSENT_TEXT, campaign=_clean(payload.get("source"), 120) or None, extra=extra))
    return 200, {"ok": True}                # same answer for stored / duplicate / suppressed: never reveal lead state


def widget_js(base_url: str) -> str:
    base, consent = json.dumps(base_url.rstrip("/")), json.dumps(CONSENT_TEXT)
    return """(function(){
var BASE=%s,CONSENT=%s,S=document.currentScript,SRC=(S&&S.getAttribute('data-source'))||location.pathname;
var host=document.getElementById('digitalburj-leads');if(!host){return;}
function el(t,a,x){var e=document.createElement(t);for(var k in(a||{}))e.setAttribute(k,a[k]);if(x)e.textContent=x;return e;}
var f=el('form',{style:'font:14px system-ui,sans-serif;color:#0f1714;background:#f6f5f1;border:1px solid #e2e0d8;border-radius:12px;padding:16px;max-width:420px'});
var fields={};
[['name','Your name','text'],['phone','Phone / WhatsApp','tel'],['email','Email','email'],['area','Preferred area','text'],['budget','Budget (AED)','text']].forEach(function(r){
  var i=el('input',{type:r[2],placeholder:r[1],'aria-label':r[1],style:'display:block;width:100%%;margin:0 0 8px;padding:8px;border:1px solid #cfcdc4;border-radius:8px'});fields[r[0]]=i;f.appendChild(i);});
var sel=el('select',{'aria-label':'Looking to',style:'display:block;width:100%%;margin:0 0 8px;padding:8px;border-radius:8px'});
['buy','rent','sell'].forEach(function(v){sel.appendChild(el('option',{value:v},'I want to '+v));});f.appendChild(sel);
var hp=el('input',{type:'text',name:'website',tabindex:'-1',autocomplete:'off','aria-hidden':'true',style:'position:absolute;left:-9999px'});f.appendChild(hp);
var lab=el('label',{style:'display:flex;gap:8px;font-size:12px;margin:6px 0 10px'});var cb=el('input',{type:'checkbox'});lab.appendChild(cb);lab.appendChild(el('span',{},CONSENT));f.appendChild(lab);
var btn=el('button',{type:'submit',style:'background:#0f1714;color:#f6f5f1;border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer'},'Request a call-back');f.appendChild(btn);
var msg=el('p',{role:'status',style:'margin:8px 0 0;font-size:13px'});f.appendChild(msg);
f.addEventListener('submit',function(e){e.preventDefault();
  if(!cb.checked){msg.textContent='Please tick the consent box to continue.';return;}
  var p={consent:true,website:hp.value,source:SRC,looking_to:sel.value};for(var k in fields)p[k]=fields[k].value;
  btn.disabled=true;
  fetch(BASE+'/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}).then(function(r){return r.json().then(function(j){return[r.ok,j];});})
  .then(function(x){msg.textContent=x[0]?'Thank you - an agent will contact you shortly.':(x[1].error||'Something went wrong.');if(x[0])f.reset();})
  .catch(function(){msg.textContent='Could not send. Please try again.';}).then(function(){btn.disabled=false;});});
host.appendChild(f);})();
""" % (base, consent)
