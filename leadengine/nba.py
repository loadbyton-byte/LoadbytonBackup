"""Next-best-action: rule-based follow-up tasks per lead. Every channel-based action carries its compliance verdict;
nothing here sends anything — it tells an agent what to do next and whether they're allowed to."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .compliance import GST, Compliance
from .db import DB
from .inventory import match
from .models import utcnow

OPEN = ("new", "contacted", "qualified", "viewing")
CADENCE_DAYS = (2, 5, 10)             # nurture follow-ups after first contact (max 3)
QUIET_DAYS = 14
SYSTEM_KINDS = ("assigned", "escalated", "flagged")
TIER_RANK = {"HOT": 0, "WARM": 1, "NURTURE": 2}


def _dt(s: str | datetime) -> datetime:
    d = datetime.fromisoformat(s) if isinstance(s, str) else s
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pick(comp: Compliance, lead: dict, prefs: list[str], now: datetime):
    """First permitted channel in preference order; if none is permitted, the most-preferred one with its verdict."""
    first = None
    for ch in prefs:
        d = comp.can_contact(lead, ch, now)
        first = first or (ch, d)
        if d.allowed:
            return ch, d
    return first


def _action(action: str, reason: str, due: datetime, channel: str | None = None, verdict=None) -> dict:
    allowed = True if verdict is None else verdict.allowed
    if verdict is not None and not verdict.allowed and verdict.retry_after:
        due = max(due, verdict.retry_after)
    return {"action": action, "reason": reason, "due": due.isoformat(), "channel": channel, "allowed": allowed,
            "compliance_reason": verdict.reason if verdict is not None else None}


def next_actions(db: DB, comp: Compliance, lead: dict, now: datetime | None = None) -> list[dict]:
    now = _dt(now or utcnow())
    if lead["status"] not in OPEN:
        return []
    rows = db.q("SELECT kind, ts FROM activities WHERE lead_id=? ORDER BY ts", (lead["id"],))
    contacted = [_dt(r["ts"]) for r in rows if r["kind"] == "status:contacted"]
    follow_ups = [r for r in rows if r["kind"] == "follow_up"]
    human = [_dt(r["ts"]) for r in rows if r["kind"] not in SYSTEM_KINDS]
    out: list[dict] = []

    if lead["tier"] in ("HOT", "WARM") and lead.get("assigned_agent_id") is None and lead["status"] == "new":
        out.append(_action("assign", f"{lead['tier']} lead has no owner", now))

    if lead["status"] == "new" and lead.get("sla_due_at"):
        due = _dt(lead["sla_due_at"])
        if due <= now:
            late = int((now - due).total_seconds() // 60)
            out.append(_action("call", f"Reply SLA overdue by {late} min - first contact now", due, "call", comp.can_contact(lead, "call", now)))
        else:
            ch, v = _pick(comp, lead, ["whatsapp", "call", "email", "public_reply"], now)
            out.append(_action("first_response", f"Reply within SLA (due {due.astimezone(GST):%H:%M} UAE time)", due, ch, v))

    if not any(r["kind"] == "matches_sent" for r in rows):
        found = match(db, lead, comp.s.golden_visa_min_aed, limit=3)
        if found:
            ch, v = _pick(comp, lead, ["whatsapp", "email"], now)
            names = ", ".join(f"{m['project']} ({m['score']})" for m in found)
            out.append(_action("send_matches", f"Inventory matches: {names}", now, ch, v))

    if contacted and len(follow_ups) < len(CADENCE_DAYS) and lead["status"] in ("contacted", "qualified", "viewing"):
        due = contacted[0] + timedelta(days=CADENCE_DAYS[len(follow_ups)])
        if now >= due:
            ch, v = _pick(comp, lead, ["whatsapp", "email", "call"], now)
            out.append(_action("follow_up", f"Nurture follow-up {len(follow_ups) + 1} of {len(CADENCE_DAYS)}", due, ch, v))

    if lead["label"] in ("seller", "landlord") and lead["status"] in ("new", "contacted", "qualified"):
        ch, v = _pick(comp, lead, ["call", "whatsapp"], now)
        out.append(_action("book_valuation_visit", "Seller/landlord: offer a free valuation visit", now, ch, v))

    last = max(human, default=_dt(lead["first_seen"]) if lead.get("first_seen") else None)
    if lead["status"] != "new" and last and now - last >= timedelta(days=QUIET_DAYS) and not any(a["action"] == "follow_up" for a in out):
        ch, v = _pick(comp, lead, ["whatsapp", "email", "call"], now)
        out.append(_action("reengage", f"No activity for {(now - last).days} days", now, ch, v))
    return out


def _lead_dict(row) -> dict:
    d = dict(row)
    d["platforms"] = json.loads(d["platforms"] or "[]")
    d["profile"] = json.loads(d["profile"] or "{}")
    return d


def todays_tasks(db: DB, comp: Compliance, agent_id: int | None, now: datetime | None = None) -> list[dict]:
    now = _dt(now or utcnow())
    end = now.astimezone(GST).replace(hour=23, minute=59, second=59, microsecond=0)
    marks = ",".join("?" * len(OPEN))
    sql, args = f"SELECT * FROM leads WHERE status IN ({marks}) AND tier != 'DISCARD'", list(OPEN)
    if agent_id is not None:
        sql, args = sql + " AND assigned_agent_id=?", args + [agent_id]
    tasks = []
    for row in db.q(sql, args):
        lead = _lead_dict(row)
        for a in next_actions(db, comp, lead, now):
            if _dt(a["due"]) <= end:
                tasks.append({"lead_id": lead["id"], "display_name": lead["display_name"], "tier": lead["tier"],
                              "score": lead["score"], **a})
    return sorted(tasks, key=lambda t: (TIER_RANK.get(t["tier"], 3), _dt(t["due"]), -t["score"]))
