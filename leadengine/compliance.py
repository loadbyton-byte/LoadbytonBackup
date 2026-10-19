"""Compliance-by-design for UAE real-estate lead generation.

What this module enforces in code (not just in a policy doc):
  * Connectors must declare a lawful acquisition basis; login-wall scraping is not an option.
  * A public social post is NOT marketing consent: direct channels stay locked until opt-in.
  * Opt-out / erasure writes a one-way hash to a suppression list so the person is never re-ingested.
  * Quiet hours (UAE time) and a Do-Not-Call list gate calls/SMS/WhatsApp.
  * Retention windows purge stale public-signal data (data minimisation).
  * Every decision is written to an audit log.

This is engineering guidance, not legal advice: confirm retention, consent wording, calling hours and
DNCR handling with UAE counsel for your licence type (PDPL — Federal Decree-Law 45/2021; TDRA telemarketing
rules; DLD/RERA advertising rules incl. Trakheesi permits).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .config import Settings
from .db import DB
from .identity import hash_identifier, normalize_email, normalize_handle, normalize_phone

GST = timezone(timedelta(hours=4))  # UAE has no DST


class Basis(str, Enum):
    OFFICIAL_API = "official_api"            # platform-sanctioned API under an accepted developer agreement
    FIRST_PARTY_FORM = "first_party_form"    # lead-gen form / webhook the person filled in with consent
    LICENSED_PROVIDER = "licensed_provider"  # data vendor whose licence permits this use
    USER_UPLOAD = "user_upload"              # your own CRM export / event list (you hold the consent record)


ALLOWED_BASES = {b.value for b in Basis}
CHANNELS = ("public_reply", "dm", "whatsapp", "sms", "call", "email")
TIMED_CHANNELS = {"whatsapp", "sms", "call"}
DNC_CHANNELS = {"sms", "call"}


class ComplianceError(Exception):
    pass


def assert_connector_allowed(name: str, basis: str) -> None:
    if basis not in ALLOWED_BASES:
        raise ComplianceError(
            f"Connector '{name}' declares basis '{basis}'. Allowed: {sorted(ALLOWED_BASES)}. "
            "Scraping behind logins, fake/sock-puppet accounts and bypassing rate limits are not supported.")


@dataclass
class Decision:
    allowed: bool
    reason: str
    retry_after: datetime | None = None

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "retry_after": self.retry_after.isoformat() if self.retry_after else None}


class Compliance:
    def __init__(self, db: DB, settings: Settings):
        self.db, self.s = db, settings
        self._dnc: set[str] | None = None

    # -- suppression / erasure -----------------------------------------------------------
    def suppress(self, identifiers: list[tuple[str, str]], reason: str) -> None:
        from .models import utcnow
        with self.db.tx() as c:
            for kind, value in identifiers:
                c.execute("INSERT OR IGNORE INTO suppression(hash,reason,created_at) VALUES(?,?,?)",
                          (hash_identifier(kind, value), reason, utcnow().isoformat()))

    def is_suppressed(self, identifiers: list[tuple[str, str]]) -> bool:
        return any(self.db.one("SELECT 1 FROM suppression WHERE hash=?", (hash_identifier(k, v),)) for k, v in identifiers)

    def suppress_raw(self, phone: str | None = None, email: str | None = None, handle: str | None = None,
                     platform: str | None = None, reason: str = "opt_out") -> list[tuple[str, str]]:
        ids: list[tuple[str, str]] = []
        if normalize_phone(phone):
            ids.append(("phone", normalize_phone(phone)))
        if normalize_email(email):
            ids.append(("email", normalize_email(email)))
        if normalize_handle(handle) and platform:
            ids.append((f"handle:{platform}", normalize_handle(handle)))
        self.suppress(ids, reason)
        return ids

    # -- DNC ------------------------------------------------------------------------------
    def dnc(self) -> set[str]:
        if self._dnc is None:
            self._dnc = set()
            if self.s.dnc_file and os.path.exists(self.s.dnc_file):
                with open(self.s.dnc_file) as f:
                    self._dnc = {n for line in f if (n := normalize_phone(line.strip()))}
        return self._dnc

    # -- the gate every outbound action must pass --------------------------------------------
    def can_contact(self, lead: dict, channel: str, now: datetime | None = None) -> Decision:
        if channel not in CHANNELS:
            return Decision(False, f"unknown channel '{channel}'")
        now = (now or datetime.now(timezone.utc)).astimezone(GST)
        if lead.get("opted_out"):
            return Decision(False, "Person opted out / requested erasure.")
        status = lead.get("consent_status", "public_signal")
        if channel == "public_reply":
            if lead.get("platforms") and status != "opted_in" and not any(
                    p in ("reddit", "facebook", "instagram", "tiktok", "linkedin", "x") for p in lead["platforms"]):
                return Decision(False, "public_reply only applies to public platform signals.")
            return Decision(True, "Public in-thread reply, human-reviewed; no personal data requested in public.")
        if status == "enquiry":
            if channel in ("sms", "dm"):
                return Decision(False, "Enquiry-only status: allowed to answer their enquiry by call/WhatsApp/email, "
                                       "not SMS or social DM. Ask for marketing opt-in before promoting other listings.")
        elif status != "opted_in":
            return Decision(False, f"No marketing opt-in on record (status='{status}'). A public post is not consent "
                                   "for direct outreach; use a public reply that invites them to opt in.")
        if channel in ("sms", "call", "whatsapp") and not lead.get("phone"):
            return Decision(False, "No phone number on record.")
        if channel == "email" and not lead.get("email"):
            return Decision(False, "No email on record.")
        if channel in DNC_CHANNELS and lead.get("phone") in self.dnc():
            return Decision(False, "Number is on the Do-Not-Call list (conservative default: blocked for calls/SMS).")
        if channel in TIMED_CHANNELS and not (self.s.contact_hour_start <= now.hour < self.s.contact_hour_end):
            nxt = now.replace(hour=self.s.contact_hour_start, minute=0, second=0, microsecond=0)
            if now.hour >= self.s.contact_hour_end:
                nxt += timedelta(days=1)
            return Decision(False, f"Outside permitted contact hours {self.s.contact_hour_start:02d}:00-"
                                   f"{self.s.contact_hour_end:02d}:00 UAE time.", nxt)
        return Decision(True, "Enquiry: limited to responding to their request." if status == "enquiry" else "Opt-in on record; channel permitted.")

    # -- retention ---------------------------------------------------------------------------
    def purge_expired(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        public_cut = (now - timedelta(days=self.s.retention_days_public)).isoformat()
        optin_cut = (now - timedelta(days=self.s.retention_days_optin)).isoformat()
        with self.db.tx() as c:
            n = c.execute("DELETE FROM leads WHERE consent_status!='opted_in' AND last_seen < ?", (public_cut,)).rowcount
            m = c.execute("DELETE FROM leads WHERE consent_status='opted_in' AND last_seen < ?", (optin_cut,)).rowcount
        self.db.audit("system", "retention_purge", None, public_deleted=n, optin_deleted=m)
        return {"public_deleted": n, "optin_deleted": m}
