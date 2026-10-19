"""Viewing scheduler with per-agent overlap detection and RFC 5545 (.ics) export."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from .compliance import GST
from .db import DB
from .models import utcnow


def feed_token(secret: str, agent_id: int) -> str:
    """Read-only calendar-feed token for ONE agent. Derived with HMAC so it can't be turned back into the API key,
    and a leaked feed URL exposes only that agent's viewings."""
    return hmac.new(secret.encode(), f"ics:{agent_id}".encode(), hashlib.sha256).hexdigest()[:40]


class ViewingConflict(Exception):
    pass


def _utc(dt: datetime) -> datetime:
    """Naive datetimes are read as UAE time (UTC+4, no DST); everything is stored in UTC."""
    return (dt.replace(tzinfo=GST) if dt.tzinfo is None else dt).astimezone(timezone.utc)


def schedule(db: DB, lead_id: int, start: datetime, *, duration_min: int = 30, location: str = "", agent_id: int | None = None) -> int:
    if not 5 <= duration_min <= 480:
        raise ValueError("duration_min must be between 5 and 480")
    lead = db.one("SELECT assigned_agent_id, status FROM leads WHERE id=?", (lead_id,))
    if not lead:
        raise LookupError("lead not found")
    agent_id = agent_id if agent_id is not None else lead["assigned_agent_id"]
    s = _utc(start)
    e = s + timedelta(minutes=duration_min)
    if agent_id is not None:
        for v in db.q("SELECT start_ts, duration_min FROM viewings WHERE agent_id=? AND status='scheduled'", (agent_id,)):
            vs = datetime.fromisoformat(v["start_ts"])
            if s < vs + timedelta(minutes=v["duration_min"]) and vs < e:
                raise ViewingConflict(f"agent {agent_id} already has a viewing at {vs.isoformat()}")
    with db.tx() as c:
        vid = c.execute("INSERT INTO viewings(lead_id,agent_id,start_ts,duration_min,location,created_at) VALUES(?,?,?,?,?,?)",
                        (lead_id, agent_id, s.isoformat(), duration_min, location[:300], utcnow().isoformat())).lastrowid
        if lead["status"] in ("new", "contacted", "qualified"):
            c.execute("UPDATE leads SET status='viewing' WHERE id=?", (lead_id,))
        c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)",
                  (lead_id, utcnow().isoformat(), "viewing_scheduled", s.isoformat()))
    return vid


def list_viewings(db: DB, agent_id: int | None = None, since: datetime | None = None) -> list[dict]:
    where, args = ["v.status='scheduled'"], []
    if agent_id is not None:
        where.append("v.agent_id=?")
        args.append(agent_id)
    if since is not None:
        where.append("v.start_ts >= ?")
        args.append(_utc(since).isoformat())
    rows = db.q("SELECT v.*, l.display_name lead_name, a.name agent_name FROM viewings v JOIN leads l ON l.id=v.lead_id "
                f"LEFT JOIN agents a ON a.id=v.agent_id WHERE {' AND '.join(where)} ORDER BY v.start_ts", args)
    return [dict(r) for r in rows]


def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """Fold to <=75 octets per physical line (continuations start with one space), never splitting a UTF-8 character."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode())
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur, size = cur + ch, size + n
    out.append(cur)
    return out


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def to_ics(rows: list[dict], *, prodid: str = "-//DigitalBurj Leads//EN") -> str:
    now = _stamp(utcnow())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", "CALSCALE:GREGORIAN", "X-WR-CALNAME:DigitalBurj Viewings"]
    for v in rows:
        start = datetime.fromisoformat(v["start_ts"])
        who = v.get("lead_name") or f"Lead #{v['lead_id']}"
        lines += ["BEGIN:VEVENT", f"UID:viewing-{v['id']}@digitalburj-leads", f"DTSTAMP:{now}", f"DTSTART:{_stamp(start)}",
                  f"DTEND:{_stamp(start + timedelta(minutes=v['duration_min']))}", f"SUMMARY:{_esc('Viewing: ' + who)}",
                  f"DESCRIPTION:{_esc('DigitalBurj Leads #' + str(v['lead_id']))}"]
        if v.get("location"):
            lines.append(f"LOCATION:{_esc(v['location'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(folded + "\r\n" for line in lines for folded in _fold(line))
