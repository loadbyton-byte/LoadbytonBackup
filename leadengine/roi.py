"""Spend and per-source ROI. Ratios are None (never an error) when their denominator is zero."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from .db import DB
from .models import utcnow
from .scoring import SLA_MINUTES

QUALIFIED = ("HOT", "WARM")


def record_spend(db: DB, source: str, campaign: str | None, period: str, amount_aed: float) -> int:
    if amount_aed < 0:
        raise ValueError("amount_aed must be >= 0")
    with db.tx() as c:
        return c.execute("INSERT INTO spend(source,campaign,period,amount_aed) VALUES(?,?,?,?)",
                         (source, campaign, period, amount_aed)).lastrowid


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 2) if den else None


def _norm(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def roi_report(db: DB, now: datetime | None = None, period: str | None = None) -> list[dict]:
    """Per-source ROI. `period` ('YYYY-MM') limits both leads (by first-seen month) and spend (by its period label)."""
    now = now or utcnow()
    first_signal = {r["lead_id"]: r["platform"] for r in db.q(
        "SELECT lead_id, platform FROM signals WHERE id IN (SELECT MIN(id) FROM signals GROUP BY lead_id)")}
    contacted = {r["lead_id"]: r["ts"] for r in db.q(
        "SELECT lead_id, MIN(ts) ts FROM activities WHERE kind='status:contacted' GROUP BY lead_id")}
    platforms = set(first_signal.values())

    def canonical(source: str) -> str:
        s = _norm(source)
        if s in platforms:
            return s
        tails = [p for p in platforms if p.endswith(":" + s)]          # "Bayut" -> "portal:bayut"
        return tails[0] if len(tails) == 1 else s

    spend: dict[str, float] = {}
    for r in db.q("SELECT source, period, amount_aed FROM spend"):
        if period is None or r["period"] == period:
            key = canonical(r["source"])
            spend[key] = spend.get(key, 0.0) + r["amount_aed"]
    stats: dict[str, dict] = {s: {"leads": 0, "qualified": 0, "won": 0, "sla_den": 0, "sla_ok": 0, "resp": []} for s in spend}
    for lead in db.q("SELECT id, tier, status, first_seen FROM leads"):
        src = first_signal.get(lead["id"])
        if src is None or (period is not None and not lead["first_seen"].startswith(period)):
            continue
        s = stats.setdefault(src, {"leads": 0, "qualified": 0, "won": 0, "sla_den": 0, "sla_ok": 0, "resp": []})
        s["leads"] += 1
        s["qualified"] += lead["tier"] in QUALIFIED
        s["won"] += lead["status"] == "won"
        seen = datetime.fromisoformat(lead["first_seen"])
        sla = SLA_MINUTES.get(lead["tier"])
        ts = contacted.get(lead["id"])
        minutes = (datetime.fromisoformat(ts) - seen).total_seconds() / 60 if ts else None
        if minutes is not None:
            s["resp"].append(minutes)
        if sla is not None and (minutes is not None or seen + timedelta(minutes=sla) < now):
            s["sla_den"] += 1
            s["sla_ok"] += minutes is not None and minutes <= sla
    out = []
    for source, s in sorted(stats.items()):
        total = float(spend.get(source) or 0)
        paid = total > 0
        out.append({
            "source": source, "leads": s["leads"], "qualified": s["qualified"], "won": s["won"], "spend_aed": total,
            "sla_ok": s["sla_ok"], "sla_den": s["sla_den"],
            "within_sla_pct": round(100 * s["sla_ok"] / s["sla_den"]) if s["sla_den"] else None,
            "median_first_response_min": round(statistics.median(s["resp"]), 1) if s["resp"] else None,
            "cost_per_lead": _ratio(total, s["leads"]) if paid else None,
            "cost_per_qualified": _ratio(total, s["qualified"]) if paid else None,
            "cost_per_won": _ratio(total, s["won"]) if paid else None,
            "win_rate_pct": round(100 * s["won"] / s["leads"]) if s["leads"] else None,
        })
    return out
