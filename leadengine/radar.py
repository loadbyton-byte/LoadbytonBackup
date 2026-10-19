"""Demand Radar: aggregate, de-identified demand by community — which areas are heating up *before* listings reflect it."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .db import DB
from .models import utcnow


def demand_radar(db: DB, days: int = 14, now: datetime | None = None, top: int = 20) -> list[dict]:
    now = now or utcnow()
    cur_start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    rows = db.q("SELECT created_at,label,areas,budget_max,intent FROM signals WHERE created_at >= ?", (prev_start.isoformat(),))
    cur: dict = defaultdict(lambda: {"n": 0, "buy": 0, "rent": 0, "sell": 0, "other": 0, "budgets": []})
    prev: dict = defaultdict(int)
    for r in rows:
        areas = json.loads(r["areas"] or "[]")
        if not areas:
            emirates = json.loads(r["intent"]).get("emirates") or []
            areas = [f"{emirates[0]} (area not stated)"] if emirates else []
        in_cur = r["created_at"] >= cur_start.isoformat()
        for a in areas:
            if not in_cur:
                prev[a] += 1
                continue
            d = cur[a]
            d["n"] += 1
            d["buy" if r["label"] in ("buyer", "investor") else "rent" if r["label"] == "renter"
              else "sell" if r["label"] in ("seller", "landlord") else "other"] += 1
            if r["budget_max"]:
                d["budgets"].append(r["budget_max"])
    out = []
    for area, d in cur.items():
        p = prev.get(area, 0)
        growth = (d["n"] - p) / p if p else None
        out.append({
            "area": area, "signals": d["n"], "previous_period": p,
            "growth_pct": round(growth * 100) if growth is not None else None,
            "buy_share_pct": round(100 * d["buy"] / d["n"]), "rent_share_pct": round(100 * d["rent"] / d["n"]),
            "supply_share_pct": round(100 * d["sell"] / d["n"]),
            "median_budget_aed": int(statistics.median(d["budgets"])) if d["budgets"] else None,
            "demand_index": round(d["n"] + 0.5 * max(d["n"] - p, 0), 1),
            "trend": "new" if p == 0 else "heating" if growth > 0.25 else "cooling" if growth < -0.25 else "steady",
        })
    return sorted(out, key=lambda x: x["demand_index"], reverse=True)[:top]
