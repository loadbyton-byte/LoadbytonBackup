"""Agent routing: weighted assignment with area/language preference, and SLA escalation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .db import DB
from .models import utcnow
from .scoring import SLA_MINUTES

OPEN_STATUSES = ("new", "contacted", "qualified", "viewing")
MAX_HOPS = 2          # reassignments before a lead is flagged for the manager
FLAGGED_LEVEL = MAX_HOPS + 1


class Distribution:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------------ agents
    def add_agent(self, name: str, *, phone: str | None = None, email: str | None = None, areas: tuple[str, ...] = (),
                  languages: tuple[str, ...] = ("en",), weight: int = 1, max_open: int = 25) -> int:
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO agents(name,phone,email,areas,languages,weight,max_open) VALUES(?,?,?,?,?,?,?)",
                            (name, phone, email, json.dumps(list(areas)), json.dumps(list(languages)), max(weight, 1), max_open))
            return cur.lastrowid

    def open_counts(self) -> dict[int, int]:
        marks = ",".join("?" * len(OPEN_STATUSES))
        rows = self.db.q(f"SELECT assigned_agent_id a, COUNT(*) n FROM leads WHERE assigned_agent_id IS NOT NULL "
                         f"AND status IN ({marks}) GROUP BY assigned_agent_id", OPEN_STATUSES)
        return {r["a"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ assignment
    def _pick(self, lead, exclude: int | None = None) -> int | None:
        counts = self.open_counts()
        lead_lang = lead["language"] or "en"
        want_langs = {"ar"} if lead_lang in ("ar", "mixed") else {"en"}
        best, best_key = None, None
        for a in self.db.q("SELECT * FROM agents WHERE active=1"):
            if a["id"] == exclude or counts.get(a["id"], 0) >= a["max_open"]:
                continue
            area_match = bool(lead["primary_area"]) and lead["primary_area"] in json.loads(a["areas"] or "[]")
            lang_match = bool(want_langs & set(json.loads(a["languages"] or "[]")))
            key = (not area_match, not lang_match, counts.get(a["id"], 0) / a["weight"], a["id"])
            if best_key is None or key < best_key:
                best, best_key = a["id"], key
        return best

    def assign(self, lead_id: int, *, exclude: int | None = None, actor: str = "system") -> int | None:
        lead = self.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not lead:
            return None
        agent = self._pick(lead, exclude)
        if agent is None:
            return None
        with self.db.tx() as c:
            c.execute("UPDATE leads SET assigned_agent_id=? WHERE id=?", (agent, lead_id))
            c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)",
                      (lead_id, utcnow().isoformat(), "assigned", f"agent:{agent} by {actor}"))
        return agent

    # ------------------------------------------------------------------ escalation
    def escalate_overdue(self, now: datetime | None = None) -> dict:
        """Unanswered HOT/WARM leads past their SLA move to the next eligible agent (max 2 hops), then are flagged."""
        now = now or utcnow()
        reassigned = flagged = 0
        rows = self.db.q("SELECT * FROM leads WHERE status='new' AND tier IN ('HOT','WARM') AND sla_due_at IS NOT NULL "
                         "AND sla_due_at < ? ORDER BY score DESC", (now.isoformat(),))
        for lead in rows:
            level = lead["escalation_level"] or 0
            if level >= MAX_HOPS:
                flagged += 1
                if level < FLAGGED_LEVEL:
                    self._set_level(lead["id"], FLAGGED_LEVEL, "flagged", "manager attention: SLA missed after max reassignments")
                continue
            current = lead["assigned_agent_id"]
            agent = self._pick(lead, exclude=current)
            if agent is None:
                flagged += 1
                continue
            sla = now + timedelta(minutes=SLA_MINUTES[lead["tier"]])
            with self.db.tx() as c:
                c.execute("UPDATE leads SET assigned_agent_id=?, escalation_level=?, sla_due_at=? WHERE id=?",
                          (agent, level + 1, sla.isoformat(), lead["id"]))
                c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)",
                          (lead["id"], now.isoformat(), "escalated", f"from agent:{current} to agent:{agent} (hop {level + 1})"))
            reassigned += 1
        return {"reassigned": reassigned, "flagged": flagged}

    def _set_level(self, lead_id: int, level: int, kind: str, note: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE leads SET escalation_level=? WHERE id=?", (level, lead_id))
            c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)", (lead_id, utcnow().isoformat(), kind, note))
