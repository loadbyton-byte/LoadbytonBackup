"""Own-data market report: where a workspace's leads come from, how each source market converts and costs, and where to focus next.

Pure over a workspace DB and the city catalogue; returns aggregate counts only (never anything about an individual lead).
Editorial tiers are *priors*, blended with measured data by shrinkage so a market with few leads is not judged on noise."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..compliance import GST
from ..db import DB
from ..models import utcnow
from ..roi import QUALIFIED
from .cities import COUNTRIES, City, catalogue

PRIOR = {1: 80, 2: 55, 3: 30}           # editorial starting priority by catalogue tier (a prior, not a statistic)
SHRINK_K = 10                            # weight of the prior, in "leads"
MEASURED_MIN = 10                        # leads needed before a market counts as measured
DOMESTIC = "AE"
HOURS = (9, 20)                          # contact window, local time
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def contact_window(tz: str | None, now: datetime) -> str | None:
    """09:00-20:00 in the market's local time, expressed in UAE time for the instant `now`."""
    if not tz:
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:
        return None
    local_day = now.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    start, end = (local_day.replace(hour=h) for h in HOURS)
    f = lambda d: d.astimezone(GST).strftime("%H:%M")
    return f"{f(start)}–{f(end)} UAE"


def _best_city(cities: list[City]) -> City | None:
    return sorted(cities, key=lambda c: (c.tier, c.name))[0] if cities else None


def _r(x: float) -> int:
    """Round half up (not Python's banker's rounding) for the percentages people read."""
    return int(x + 0.5)


def _row(id_: str, name: str, country: str, leads: list[dict], known_total: int, spend: dict, ref: City | None, now: datetime,
         growth: bool = True) -> dict:
    n = len(leads)
    qualified = sum(1 for x in leads if x["tier"] in QUALIFIED)
    won = sum(1 for x in leads if x["status"] == "won")
    cur = sum(1 for x in leads if x["age_days"] < 28)
    prev = sum(1 for x in leads if 28 <= x["age_days"] < 56)
    total = spend.get("amount", 0.0)
    tagged = spend.get("tagged", False)
    ratio = lambda den: round(total / den, 2) if tagged and den else None
    return {
        "id": id_, "name": name, "country": country, "leads": n, "qualified": qualified, "won": won,
        "avg_score": round(sum(x["score"] for x in leads) / n, 1) if n else None,
        "share_pct": _r(100 * n / known_total) if known_total else 0,
        "growth_pct": _r(100 * (cur - prev) / prev) if growth and prev >= 3 else None,         # windows run back from `now`: meaningless inside a past period
        "spend_aed": round(total, 2), "cost_per_lead": ratio(n), "cost_per_qualified": ratio(qualified), "cost_per_won": ratio(won),
        "language": ref.languages[0] if ref and ref.languages else None, "currency": ref.currency if ref else None,
        "timezone": ref.timezone if ref else None, "contact_window": contact_window(ref.timezone, now) if ref else None,
        "tier": ref.tier if ref else None,
    }


def market_report(db: DB, now: datetime | None = None, period: str | None = None, limit: int | None = 25) -> dict:
    """`limit=None` returns every row (used by the per-country drill-down)."""
    if period is not None and not _PERIOD.match(period):
        raise ValueError("period must look like 2026-10")
    now = now or utcnow()
    cat = catalogue()
    by_id = {c.id: c for c in cat}
    by_country: dict[str, list[City]] = {}
    for c in cat:
        by_country.setdefault(c.country, []).append(c)

    leads = []
    for r in db.q("SELECT origin_country, origin_city, tier, status, score, first_seen FROM leads"):
        if period is not None and not (r["first_seen"] or "").startswith(period):
            continue
        age = (now - datetime.fromisoformat(r["first_seen"])).total_seconds() / 86400 if r["first_seen"] else 9999
        leads.append({"country": r["origin_country"], "city": r["origin_city"], "tier": r["tier"], "status": r["status"],
                      "score": r["score"] or 0, "age_days": age})
    known = [x for x in leads if x["country"]]

    spend_city: dict[str, float] = {}
    spend_country: dict[str, float] = {}
    unallocated = 0.0
    for r in db.q("SELECT country, city, amount_aed, period FROM spend"):
        if period is not None and r["period"] != period:
            continue
        if r["city"]:
            spend_city[r["city"]] = spend_city.get(r["city"], 0.0) + r["amount_aed"]
            spend_country[r["country"]] = spend_country.get(r["country"], 0.0) + r["amount_aed"]
        elif r["country"]:
            spend_country[r["country"]] = spend_country.get(r["country"], 0.0) + r["amount_aed"]
        else:
            unallocated += r["amount_aed"]

    by_cc: dict[str, list[dict]] = {}
    by_cid: dict[str, list[dict]] = {}
    for x in known:
        by_cc.setdefault(x["country"], []).append(x)
        if x["city"]:
            by_cid.setdefault(x["city"], []).append(x)
    intl_total = sum(len(g) for cc, g in by_cc.items() if cc != DOMESTIC)
    unlocated = {cc: sum(1 for x in g if not x["city"]) for cc, g in by_cc.items()}
    growth_ok = period is None

    countries, cities = [], []
    for cc in sorted(set(by_cc) | set(spend_country)):
        group = by_cc.get(cc, [])
        ref = _best_city(by_country.get(cc, []))
        row = _row(cc, COUNTRIES.get(cc, (cc, ""))[0], cc, group, len(known), {"amount": spend_country.get(cc, 0.0), "tagged": cc in spend_country}, ref, now, growth_ok)
        row["intl_share_pct"] = _r(100 * len(group) / intl_total) if cc != DOMESTIC and intl_total else None       # share among international leads (ranking basis)
        tops = sorted(((sum(1 for x in group if x["city"] == c), c) for c in {x["city"] for x in group if x["city"]}), reverse=True)
        row["top_city"] = by_id[tops[0][1]].name if tops and tops[0][1] in by_id else None
        countries.append(row)
    for cid in sorted(set(by_cid) | set(spend_city)):
        city = by_id.get(cid)
        if not city:
            continue
        cities.append(_row(cid, city.name, city.country, by_cid.get(cid, []), len(known), {"amount": spend_city.get(cid, 0.0), "tagged": cid in spend_city}, city, now, growth_ok))
    countries.sort(key=lambda r: (-r["leads"], r["name"]))
    cities.sort(key=lambda r: (-r["leads"], r["name"]))

    focus = []
    for r in countries:
        if r["country"] == DOMESTIC or r["tier"] is None or r["leads"] == 0:           # spend with no leads is not a market to chase: it is a question
            continue
        n = r["leads"]
        quality = sum(1 for x in by_cc[r["id"]] if x["tier"] in QUALIFIED or x["status"] == "won") / n
        share = r["intl_share_pct"] or 0
        measured = 100 * (0.6 * min(1.0, share / 25) + 0.4 * quality)
        prior = PRIOR[r["tier"]]
        basis = "measured" if n >= MEASURED_MIN else "editorial"
        reasons = [k for k, ok in (("high_share", share >= 25), ("high_quality", quality >= 0.5),
                                   ("growing", r["growth_pct"] is not None and r["growth_pct"] >= 25),
                                   ("low_sample", n < MEASURED_MIN), ("editorial_prior", basis == "editorial")) if ok]
        focus.append({**r, "score": round((n * measured + SHRINK_K * prior) / (n + SHRINK_K), 1), "basis": basis, "reasons": reasons})
    focus.sort(key=lambda r: (-r["score"], -r["leads"], r["name"]))

    have = set(by_cid)
    active = {cc for cc, n in unlocated.items() if n >= 3 and cc != DOMESTIC}       # leads there with no city may well be in any of its cities
    untapped = [{"id": c.id, "name": c.name, "country": c.country, "language": c.languages[0] if c.languages else None, "timezone": c.timezone,
                 "contact_window": contact_window(c.timezone, now), "tier": c.tier}
                for c in sorted(cat, key=lambda c: (c.tier, c.name)) if c.tier <= 2 and c.country != DOMESTIC and c.id not in have and c.country not in active][:10]
    return {"known_origin_pct": _r(100 * len(known) / len(leads)) if leads else None, "total_leads": len(leads),
            "unallocated_spend_aed": round(unallocated, 2), "countries": countries[:limit], "cities": cities[:limit], "focus": focus[:limit],
            "untapped": untapped, "generated_at": now.isoformat()}
