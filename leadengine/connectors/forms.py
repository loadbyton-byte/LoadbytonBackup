"""First-party lead capture: Meta (Facebook + Instagram) Lead Ads, TikTok / LinkedIn lead forms, portals, CSV.

These are the highest-value, lowest-risk sources: the person typed their own details into a form."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import re
from datetime import datetime, timezone
from typing import Iterable

from ..config import Settings
from ..models import Signal
from .base import Connector, http_json

GRAPH = "https://graph.facebook.com/v21.0"
_ALIASES = {
    "name": ("full_name", "name", "fullname", "full name", "your name"),
    "first": ("first_name", "firstname"),
    "last": ("last_name", "lastname"),
    "phone": ("phone_number", "phone", "mobile", "mobile_number", "whatsapp", "contact_number"),
    "email": ("email", "work_email", "email_address"),
    "consent": ("consent", "opt_in", "marketing_consent", "agree", "i_agree", "contact_me"),
}


def verify_hmac(secret: str, body: bytes, signature: str, prefix: str = "sha256=") -> bool:
    if not secret or not signature:
        return False
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _get(fields: dict[str, str], key: str) -> str | None:
    for a in _ALIASES[key]:
        if fields.get(a):
            return fields[a]
    return None


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "agree", "i agree", "accepted")


def fields_to_signal(platform: str, external_id: str, fields: dict[str, str], *, source_type: str = "lead_form",
                     created_at: datetime | None = None, campaign: str | None = None, consent_text: str | None = None,
                     url: str | None = None) -> Signal:
    f = {re.sub(r"\s+", "_", k.strip().lower()): v for k, v in fields.items() if v is not None}
    name = _get(f, "name") or " ".join(x for x in (_get(f, "first"), _get(f, "last")) if x) or None
    used = {a for k in _ALIASES for a in _ALIASES[k]}
    answers = {k: v for k, v in f.items() if k not in used}       # custom questions: budget, area, buy/rent...
    consent = _truthy(_get(f, "consent"))
    return Signal(platform=platform, external_id=external_id, source_type=source_type,
                  text=" . ".join(f"{k.replace('_', ' ')}: {v}" for k, v in answers.items()),
                  author_name=name, phone=_get(f, "phone"), email=_get(f, "email"), consent_marketing=consent,
                  consent_text=consent_text if consent else None, created_at=created_at or datetime.now(timezone.utc),
                  campaign=campaign, url=url, extra=answers)


class MetaLeadAdsConnector(Connector):
    """Meta webhook 'leadgen' -> Graph API lead retrieval. Covers Facebook AND Instagram lead ads."""
    name, platform, basis = "meta_lead_ads", "facebook", "first_party_form"
    description = "Facebook/Instagram Lead Ads via webhook + Graph API (needs leads_retrieval permission)."

    def __init__(self, settings: Settings, http=http_json):
        self.s, self.http = settings, http

    def fetch(self, **_) -> Iterable[Signal]:
        return []  # push-based; see parse_webhook

    def parse_webhook(self, payload: dict) -> list[Signal]:
        out: list[Signal] = []
        for entry in payload.get("entry", []):
            for ch in entry.get("changes", []):
                if ch.get("field") != "leadgen":
                    continue
                v = ch["value"]
                lead = self.http(f"{GRAPH}/{v['leadgen_id']}?access_token={self.s.meta_page_token}")
                fields = {fd["name"]: (fd.get("values") or [""])[0] for fd in lead.get("field_data", [])}
                platform = "instagram" if str(lead.get("platform", "")).lower() in ("ig", "instagram") else "facebook"
                ts = lead.get("created_time")
                created = datetime.fromisoformat(ts.replace("+0000", "+00:00")) if ts else None
                out.append(fields_to_signal(platform, str(v["leadgen_id"]), fields, created_at=created,
                                            campaign=str(v.get("ad_id") or v.get("form_id") or ""),
                                            consent_text="Meta Lead Ad form (see form's privacy/disclaimer text)"))
        return out


class FormWebhookConnector(Connector):
    """Generic signed webhook for TikTok Instant Forms, LinkedIn Lead Gen Forms, portal pushes, your website."""
    name, platform, basis = "form_webhook", "web", "first_party_form"
    description = "HMAC-signed JSON webhook (e.g. via Zapier/Make/native integration) for TikTok, LinkedIn, website forms."

    def __init__(self, settings: Settings):
        self.s = settings

    def fetch(self, **_) -> Iterable[Signal]:
        return []

    def parse(self, platform: str, payload: dict) -> Signal:
        ext = str(payload.get("id") or payload.get("lead_id") or payload.get("submission_id")
                  or hashlib.sha1(repr(sorted(payload.items())).encode()).hexdigest()[:16])
        fields = {k: str(v) for k, v in (payload.get("fields") or payload).items() if isinstance(v, (str, int, float, bool))}
        return fields_to_signal(platform, ext, fields, campaign=payload.get("campaign"), consent_text=payload.get("consent_text"),
                                source_type="lead_form")


_PORTAL_FIELD = re.compile(r"^\s*(name|full name|phone|mobile|contact number|email|message|comments?|reference|listing ref(?:erence)?|property ref(?:erence)?)\s*[:\-]\s*(.+)$",
                           re.I | re.M)


def parse_portal_email(portal: str, subject: str, body: str, message_id: str) -> Signal:
    """Bayut / Property Finder / Dubizzle enquiry notification emails -> Signal (portal_inquiry)."""
    found: dict[str, str] = {}
    for m in _PORTAL_FIELD.finditer(body):
        key = m.group(1).lower().replace("full ", "").replace("contact number", "phone").replace("mobile", "phone")
        key = {"comment": "message", "comments": "message", "listing reference": "ref", "listing ref": "ref",
               "property reference": "ref", "property ref": "ref", "reference": "ref"}.get(key, key)
        found.setdefault(key, m.group(2).strip())
    ref = found.pop("ref", None)
    text = " . ".join(x for x in (subject, found.pop("message", None), f"listing {ref}" if ref else None) if x)
    return Signal(platform=f"portal:{portal}", external_id=message_id, source_type="portal_inquiry", text=text,
                  author_name=found.get("name"), phone=found.get("phone"), email=found.get("email"), campaign=ref)


def parse_csv(content: str, platform: str = "csv") -> list[Signal]:
    """Your own CRM/event export. Include a `consent` column (yes/no) — rows without it are treated as no consent."""
    out = []
    for i, row in enumerate(csv.DictReader(io.StringIO(content))):
        f = {k: v for k, v in row.items() if k and v}
        ext = hashlib.sha1(repr(sorted(f.items())).encode()).hexdigest()[:16]
        sig = fields_to_signal(platform, ext, f, source_type="import", consent_text="Imported with consent flag from source CRM")
        out.append(sig)
    return out
