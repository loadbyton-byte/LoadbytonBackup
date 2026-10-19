"""The agency's own project/inventory list, and lead <-> project matching."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime

from .db import DB
from .nlp.areas import extract_areas
from .nlp.intent import parse_budget
from .nlp.text import normalize

_BEDS_SPLIT = re.compile(r"[,/&;]+")


def parse_price(raw: str | None) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    plain = re.sub(r"(?i)\b(aed|dhs?|dirhams?)\b|[,\s]", "", s)
    if re.fullmatch(r"\d+(\.\d+)?", plain):
        return float(plain)
    return parse_budget(normalize(s))[1]


def parse_beds(raw: str | None) -> list[int]:
    out: set[int] = set()
    for part in _BEDS_SPLIT.split((raw or "").lower()):
        part = part.strip()
        if part == "studio":
            out.add(0)
        elif m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", part):
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def import_projects_csv(db: DB, text: str) -> dict:
    imported, skipped = 0, []
    for n, row in enumerate(csv.DictReader(io.StringIO(text)), start=1):
        name = (row.get("name") or "").strip()
        if not name:
            skipped.append((n, "missing name"))
            continue
        lo, hi = parse_price(row.get("min_price")), parse_price(row.get("max_price"))
        if lo is None and hi is None:
            skipped.append((n, "missing or unparseable price"))
            continue
        lo, hi = lo if lo is not None else hi, hi if hi is not None else lo
        if lo > hi:
            skipped.append((n, "min_price > max_price"))
            continue
        areas, emirates = extract_areas(row.get("area") or "")
        area = areas[0] if areas else (row.get("area") or "").strip() or None
        year = re.search(r"20\d\d", row.get("handover") or "")
        with db.tx() as c:
            c.execute("INSERT INTO projects(name,developer,area,emirate,min_price,max_price,bedrooms,handover_year,payment_plan) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (name, (row.get("developer") or "").strip() or None, area,
                       (row.get("emirate") or "").strip() or (emirates[0] if emirates else None), lo, hi,
                       json.dumps(parse_beds(row.get("bedrooms"))), int(year.group(0)) if year else None,
                       (row.get("payment_plan") or "").strip() or None))
        imported += 1
    return {"imported": imported, "skipped": skipped}


def _profile(lead: dict) -> dict:
    p = lead.get("profile") or {}
    return json.loads(p) if isinstance(p, str) else p


def match(db: DB, lead: dict, golden_min: float, limit: int = 5, now: datetime | None = None) -> list[dict]:
    """Rank projects for a lead. Area is a hard filter; budget, bedrooms and off-plan timing refine the score."""
    if lead.get("purchase_type") == "rent" or lead.get("label") in ("renter", "landlord"):
        return []
    now = now or datetime.now()
    areas = {a for a in [lead.get("primary_area"), *_profile(lead).get("areas", [])] if a}
    budget = lead.get("budget_max")
    out = []
    for p in db.q("SELECT * FROM projects"):
        if p["area"] not in areas:
            continue
        score, reasons = 50, [f"area: {p['area']}"]
        lo, hi = p["min_price"], p["max_price"]
        if budget:
            if lo <= budget <= hi:
                score, reasons = score + 30, reasons + ["budget within price range"]
            elif lo * 0.85 <= budget <= hi * 1.15:
                score, reasons = score + 15, reasons + ["budget within 15% of price range"]
        beds = json.loads(p["bedrooms"] or "[]")
        if lead.get("bedrooms") is not None and lead["bedrooms"] in beds:
            score, reasons = score + 10, reasons + [f"{lead['bedrooms']}BR available"]
        if lead.get("purchase_type") == "off_plan" and p["handover_year"] and p["handover_year"] >= now.year:
            score, reasons = score + 10, reasons + [f"handover {p['handover_year']}"]
        out.append({"project": p["name"], "developer": p["developer"], "area": p["area"], "min_price": lo, "max_price": hi,
                    "handover_year": p["handover_year"], "payment_plan": p["payment_plan"], "score": score, "reasons": reasons,
                    "golden_visa_fit": bool(budget and budget >= golden_min and hi >= golden_min)})
    return sorted(out, key=lambda x: (-x["score"], x["project"]))[:limit]
