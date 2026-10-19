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


MIN_PRICE = 50_000          # below this a "price" is almost certainly a data-entry error
MAX_BEDS_RANGE = 10


def parse_beds(raw: str | None) -> list[int]:
    """'studio,1,2' / '1-3' -> [0,1,2]. Oversized ranges (e.g. '0-100000000') yield [] instead of exploding."""
    out: set[int] = set()
    for part in _BEDS_SPLIT.split((raw or "").lower()):
        part = part.strip()
        if part == "studio":
            out.add(0)
        elif m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", part):
            lo, hi = int(m.group(1)), int(m.group(2))
            if not 0 <= hi - lo <= MAX_BEDS_RANGE:
                return []
            out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def import_projects_csv(db: DB, text: str) -> dict:
    """Header names are case/space-insensitive. Rows are skipped (and reported) when the column count is off, the name or price is
    missing/implausible, or bedrooms can't be understood. Re-importing updates projects keyed by (name, developer)."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower().replace(" ", "_") for h in next(reader, [])]
    imported, skipped = 0, []
    for n, cells in enumerate(reader, start=1):
        if not any(c.strip() for c in cells):
            continue
        if len(cells) != len(header):
            skipped.append((n, f"column count {len(cells)} != {len(header)} in header (quote values that contain commas)"))
            continue
        row = dict(zip(header, cells))
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
        if lo < MIN_PRICE:
            skipped.append((n, f"price below AED {MIN_PRICE:,} looks like a data error"))
            continue
        beds_raw = (row.get("bedrooms") or "").strip()
        beds = parse_beds(beds_raw)
        if beds_raw and not beds:
            skipped.append((n, "bedrooms not understood (use e.g. 'studio,1,2' or '1-3')"))
            continue
        areas, emirates = extract_areas(row.get("area") or "")
        area = areas[0] if areas else (row.get("area") or "").strip() or None
        year = re.search(r"20\d\d", row.get("handover") or "")
        dev = (row.get("developer") or "").strip() or None
        vals = (dev, area, (row.get("emirate") or "").strip() or (emirates[0] if emirates else None), lo, hi, json.dumps(beds),
                int(year.group(0)) if year else None, (row.get("payment_plan") or "").strip() or None)
        with db.tx() as c:
            existing = c.execute("SELECT id FROM projects WHERE name=? AND COALESCE(developer,'')=?", (name, dev or "")).fetchone()
            if existing:
                c.execute("UPDATE projects SET developer=?,area=?,emirate=?,min_price=?,max_price=?,bedrooms=?,handover_year=?,payment_plan=? WHERE id=?",
                          (*vals, existing["id"]))
            else:
                c.execute("INSERT INTO projects(name,developer,area,emirate,min_price,max_price,bedrooms,handover_year,payment_plan) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (name, *vals))
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
