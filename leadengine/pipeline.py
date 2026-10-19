"""ingest -> suppression check -> classify -> resolve identity -> score -> store -> aggregate."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .compliance import Compliance
from .config import Settings
from .db import DB
from .distribution import Distribution
from .identity import identifiers_for, normalize_email, normalize_phone
from .models import Signal, utcnow
from .nlp.intent import Intent, classify, fill_entities
from .scoring import SLA_MINUTES, score_signal, tier_for

LLMRefiner = Callable[[str, Intent], Intent]
FORM_SOURCES = ("lead_form", "portal_inquiry")


@dataclass
class IngestResult:
    status: str            # created | merged | duplicate | discarded | suppressed
    lead_id: int | None = None
    score: int = 0
    tier: str = "DISCARD"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Pipeline:
    def __init__(self, db: DB, settings: Settings | None = None, llm_refiner: LLMRefiner | None = None):
        self.db = db
        self.s = settings or Settings()
        self.compliance = Compliance(db, self.s)
        self.distribution = Distribution(db)
        self.llm_refiner = llm_refiner

    # ------------------------------------------------------------------ ingest
    def ingest(self, sig: Signal) -> IngestResult:
        if self.db.one("SELECT 1 FROM signals WHERE platform=? AND external_id=?", (sig.platform, sig.external_id)):
            return IngestResult("duplicate")
        ids = identifiers_for(sig.platform, sig.author_handle, sig.phone, sig.email)
        if self.compliance.is_suppressed(ids):
            self.db.bump("suppressed")
            return IngestResult("suppressed")

        it = self._classify(sig)
        if it.label in ("noise", "agent_spam"):
            self.db.bump(f"discarded_{it.label}")        # data minimisation: noise is counted, never stored
            return IngestResult("discarded")

        lead_id, created = self._resolve(sig, ids, it)
        phone, email = normalize_phone(sig.phone), normalize_email(sig.email)
        has_consent = sig.consent_marketing
        sc = score_signal(it, sig.source_type, sig.created_at, bool(phone), bool(email), has_consent)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO signals(lead_id,platform,external_id,source_type,text,url,author_handle,created_at,ingested_at,"
                "label,stage,score,areas,budget_max,campaign,intent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lead_id, sig.platform, sig.external_id, sig.source_type, sig.text[:4000], sig.url, sig.author_handle,
                 sig.created_at.isoformat(), utcnow().isoformat(), it.label, it.stage, sc.total,
                 json.dumps(it.areas), it.budget_max, sig.campaign, json.dumps(it.to_dict())))
            if sig.consent_marketing:
                c.execute("UPDATE leads SET consent_status='opted_in', consent_evidence=? WHERE id=?",
                          (json.dumps({"text": sig.consent_text, "platform": sig.platform, "at": utcnow().isoformat()}), lead_id))
            elif sig.source_type in FORM_SOURCES and (phone or email):
                # They asked to be contacted about a specific listing: reply-to-enquiry only, not general marketing.
                c.execute("UPDATE leads SET consent_status='enquiry', consent_evidence=? WHERE id=? AND consent_status='public_signal'",
                          (json.dumps({"basis": "enquiry", "platform": sig.platform, "at": utcnow().isoformat()}), lead_id))
            if sig.author_name:
                c.execute("UPDATE leads SET display_name=COALESCE(display_name,?) WHERE id=?", (sig.author_name, lead_id))
            if phone:
                c.execute("UPDATE leads SET phone=COALESCE(phone,?) WHERE id=?", (phone, lead_id))
            if email:
                c.execute("UPDATE leads SET email=COALESCE(email,?) WHERE id=?", (email, lead_id))
        self.recompute(lead_id)
        lead = self.db.one("SELECT score,tier,assigned_agent_id FROM leads WHERE id=?", (lead_id,))
        if lead["tier"] in ("HOT", "WARM") and lead["assigned_agent_id"] is None:
            self.distribution.assign(lead_id)
        self.db.audit("system", "ingest", lead_id, platform=sig.platform, source_type=sig.source_type, label=it.label)
        return IngestResult("created" if created else "merged", lead_id, lead["score"], lead["tier"])

    def ingest_many(self, signals: list[Signal]) -> dict:
        counts: Counter = Counter()
        for s in signals:
            counts[self.ingest(s).status] += 1
        return dict(counts)

    def _classify(self, sig: Signal) -> Intent:
        text = " . ".join([sig.text or "", *[f"{k.replace('_', ' ')}: {v}" for k, v in sig.extra.items()
                                          if isinstance(v, (str, int, float))]]).strip(" .")
        it = classify(text)
        if sig.source_type in FORM_SOURCES and it.label in ("noise", "agent_spam"):
            # A person who filled a lead form on a property ad/portal has intent even if the free text is empty.
            wants_rent = "rent" in text.lower() or "ايجار" in text
            it.label, it.stage, it.confidence = ("renter" if wants_rent else "buyer"), "shortlisting", 0.7
            it.purchase_type = "rent" if wants_rent else "ready"
            it.evidence = [f"{sig.source_type} submission"]
            it.language = it.language if it.language != "unknown" else "en"
            fill_entities(it, text)
            if wants_rent:
                it.purchase_type = "rent"
        if self.llm_refiner and it.label not in ("noise", "agent_spam"):
            it = self.llm_refiner(sig.text, it)
        return it

    # ------------------------------------------------------------------ identity
    def _resolve(self, sig: Signal, ids: list[tuple[str, str]], it: Intent) -> tuple[int, bool]:
        matched = sorted({r["lead_id"] for k, v in ids for r in self.db.q("SELECT lead_id FROM identifiers WHERE kind=? AND value=?", (k, v))})
        if not matched:
            matched = self._soft_match(sig, ids, it)
        created = False
        with self.db.tx() as c:
            if not matched:
                cur = c.execute("INSERT INTO leads(first_seen,last_seen,platforms) VALUES(?,?,?)",
                                (utcnow().isoformat(), utcnow().isoformat(), "[]"))
                lead_id, created = cur.lastrowid, True
            else:
                lead_id = matched[0]
                for other in matched[1:]:               # one person, two profiles => merge records
                    c.execute("UPDATE signals SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    c.execute("UPDATE OR IGNORE identifiers SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    c.execute("UPDATE activities SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    c.execute("UPDATE OR IGNORE conversations SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    c.execute("DELETE FROM leads WHERE id=?", (other,))
            for k, v in ids:
                c.execute("INSERT OR IGNORE INTO identifiers(kind,value,lead_id) VALUES(?,?,?)", (k, v, lead_id))
        return lead_id, created

    def _soft_match(self, sig: Signal, ids: list[tuple[str, str]], it: Intent) -> list[int]:
        """Probabilistic cross-platform link: the same (>=6 char) handle on another platform AND an overlapping
        community. Deliberately strict — a wrong merge is worse than a missed one."""
        found: set[int] = set()
        for kind, value in ids:
            if not kind.startswith("handle:") or len(value) < 6 or not it.areas:
                continue
            for r in self.db.q("SELECT lead_id FROM identifiers WHERE kind LIKE 'handle:%' AND kind!=? AND value=?", (kind, value)):
                areas = {a for row in self.db.q("SELECT areas FROM signals WHERE lead_id=?", (r["lead_id"],)) for a in json.loads(row["areas"] or "[]")}
                if areas & set(it.areas):
                    found.add(r["lead_id"])
        return sorted(found)

    # ------------------------------------------------------------------ aggregate
    def recompute(self, lead_id: int, now: datetime | None = None) -> None:
        """Rebuild the lead profile + score from all of its signals (also applies recency decay)."""
        now = now or utcnow()
        lead = self.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        rows = self.db.q("SELECT * FROM signals WHERE lead_id=? ORDER BY created_at", (lead_id,))
        if not lead or not rows:
            return
        consent = lead["consent_status"] == "opted_in"
        best_score, best_row, best_bd, best_sla = -1, None, {}, None
        for r in rows:
            it = Intent(**json.loads(r["intent"]))
            sc = score_signal(it, r["source_type"], datetime.fromisoformat(r["created_at"]), bool(lead["phone"]),
                              bool(lead["email"]), consent, now)
            if sc.total > best_score:
                best_score, best_row, best_bd, best_sla = sc.total, r, sc.breakdown, sc.sla_due_at
        best = json.loads(best_row["intent"])
        platforms = sorted({r["platform"] for r in rows})
        corroboration = min(10, 5 * (len(platforms) - 1))
        total = min(100, best_score + corroboration)
        best_bd["cross_platform_bonus"] = corroboration
        tier = tier_for(total)
        area_counts = Counter(a for r in rows for a in json.loads(r["areas"] or "[]"))
        latest = {k: None for k in ("budget_max", "bedrooms", "timeline_days", "financing")}
        for r in rows:                                   # later signals override earlier ones
            data = json.loads(r["intent"])
            for k in latest:
                if data.get(k) is not None:
                    latest[k] = data[k]
        top_area = area_counts.most_common(1)[0][0] if area_counts else None
        emirate = (best.get("emirates") or [None])[0]
        sla = None
        if tier in SLA_MINUTES and lead["status"] == "new":
            prev = lead["sla_due_at"]
            sla = prev or (now + timedelta(minutes=SLA_MINUTES[tier])).isoformat()
        profile = {"areas": [a for a, _ in area_counts.most_common()], "property_types": best.get("property_types", []),
                   "wants_agent": best.get("wants_agent", False), "golden_visa": best.get("golden_visa", False),
                   "evidence": best.get("evidence", []), "signals": len(rows)}
        with self.db.tx() as c:
            c.execute(
                "UPDATE leads SET label=?,stage=?,score=?,tier=?,primary_area=?,emirate=?,budget_max=?,bedrooms=?,"
                "purchase_type=?,timeline_days=?,financing=?,language=?,last_seen=?,platforms=?,breakdown=?,profile=?,sla_due_at=? WHERE id=?",
                (best["label"], best["stage"], total, tier, top_area, emirate, latest["budget_max"], latest["bedrooms"],
                 best.get("purchase_type"), latest["timeline_days"], latest["financing"], best["language"],
                 max(r["created_at"] for r in rows), json.dumps(platforms), json.dumps(best_bd), json.dumps(profile),
                 sla, lead_id))

    def rescore_all(self) -> int:
        ids = [r["id"] for r in self.db.q("SELECT id FROM leads")]
        for i in ids:
            self.recompute(i)
        return len(ids)

    # ------------------------------------------------------------------ rights & consent
    def record_consent(self, lead_id: int, evidence_text: str, actor: str = "api") -> None:
        with self.db.tx() as c:
            c.execute("UPDATE leads SET consent_status='opted_in', consent_evidence=? WHERE id=?",
                      (json.dumps({"text": evidence_text, "at": utcnow().isoformat(), "actor": actor}), lead_id))
        self.db.audit(actor, "consent_recorded", lead_id)
        self.recompute(lead_id)

    def erase(self, lead_id: int, reason: str = "erasure_request", actor: str = "api") -> bool:
        ids = [(r["kind"], r["value"]) for r in self.db.q("SELECT kind,value FROM identifiers WHERE lead_id=?", (lead_id,))]
        if not self.db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            return False
        self.compliance.suppress(ids, reason)            # remember *that* they said no, never *who* they are
        with self.db.tx() as c:
            c.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        self.db.audit(actor, "erased", None, lead=lead_id, reason=reason)
        return True

    def opt_out(self, phone: str | None = None, email: str | None = None, handle: str | None = None,
                platform: str | None = None) -> int:
        ids = self.compliance.suppress_raw(phone, email, handle, platform, reason="opt_out")
        n = 0
        for k, v in ids:
            for r in self.db.q("SELECT lead_id FROM identifiers WHERE kind=? AND value=?", (k, v)):
                n += self.erase(r["lead_id"], reason="opt_out", actor="optout")
        self.db.audit("optout", "opt_out", None, identifiers=len(ids), leads_erased=n)
        return n
