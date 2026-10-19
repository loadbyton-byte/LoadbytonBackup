"""Explainable 0-100 lead score. Every point is attributable; nothing is a black box."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import utcnow
from .nlp.intent import Intent

LABEL_WEIGHT = {"seeker": 0.7, "buyer": 1.0, "investor": 1.0, "seller": 0.95, "landlord": 0.7, "renter": 0.6}
SOURCE_POINTS = {"lead_form": 10, "portal_inquiry": 9, "public_comment": 5, "public_post": 5, "import": 4}
# Source freshness half-lives: a form fill goes cold slower than a tweet-length public post.
HALF_LIFE_DAYS = {"lead_form": 5, "portal_inquiry": 4, "public_comment": 10, "public_post": 12, "import": 30}
SLA_MINUTES = {"HOT": 5, "WARM": 60, "NURTURE": 24 * 60}
# Below these budgets a request is unrealistic for the stated type (flagged, not discarded).
MIN_PLAUSIBLE_BUY = {"villa": 900_000, "townhouse": 1_100_000, "penthouse": 2_000_000, "apartment": 350_000, "studio": 300_000}


@dataclass
class Score:
    total: int
    tier: str
    breakdown: dict
    sla_due_at: datetime | None


def tier_for(score: int) -> str:
    return "HOT" if score >= 75 else "WARM" if score >= 50 else "NURTURE" if score >= 30 else "DISCARD"


def score_signal(it: Intent, source_type: str, created_at: datetime, has_phone: bool, has_email: bool,
                 consent: bool, now: datetime | None = None) -> Score:
    now = now or utcnow()
    if it.label in ("noise", "agent_spam"):
        return Score(0, "DISCARD", {"reason": it.label}, None)

    intent_pts = 30 * it.confidence * LABEL_WEIGHT.get(it.label, 0.5)
    stage_pts = {"ready": 20, "shortlisting": 13, "research": 6}.get(it.stage, 0)
    spec = 0.0
    spec += 7 if it.budget_max else 0
    spec += 6 if it.areas else (2 if it.emirates else 0)
    spec += 4 if (it.bedrooms is not None or it.property_types) else 0
    spec += 3 if it.purchase_type else 0
    td = it.timeline_days
    urgency = 0 if td is None else 10 if td <= 7 else 8 if td <= 30 else 5 if td <= 90 else 2
    financial = {"cash": 5, "mortgage": 4, "payment_plan": 3}.get(it.financing, 0)
    source = SOURCE_POINTS.get(source_type, 4)
    contact = (5 if consent and has_phone else 3 if has_phone else 2 if has_email else 0)
    agent_bonus = 4 if it.wants_agent else 0

    raw = intent_pts + stage_pts + spec + urgency + financial + source + contact + agent_bonus
    age_days = max((now - created_at).total_seconds() / 86400, 0)
    recency = max(0.5 ** (age_days / HALF_LIFE_DAYS.get(source_type, 12)), 0.3)

    flags = []
    if it.label in ("buyer", "investor") and it.budget_max:
        floor = min((MIN_PLAUSIBLE_BUY[t] for t in it.property_types if t in MIN_PLAUSIBLE_BUY), default=None)
        if floor and it.budget_max < floor:
            raw -= 8
            flags.append(f"budget AED {it.budget_max:,.0f} is below typical floor AED {floor:,.0f} for {it.property_types}")

    total = int(round(max(0, min(raw * recency, 100))))
    tier = tier_for(total)
    sla = now + timedelta(minutes=SLA_MINUTES[tier]) if tier in SLA_MINUTES else None
    breakdown = {
        "intent": round(intent_pts, 1), "stage": stage_pts, "specificity": spec, "urgency": urgency,
        "financial_readiness": financial, "source_quality": source, "contactability": contact,
        "asked_for_agent": agent_bonus, "recency_multiplier": round(recency, 2), "flags": flags,
    }
    return Score(total, tier, breakdown, sla)
