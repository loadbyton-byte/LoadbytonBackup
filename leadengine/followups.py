"""Follow-up engine: speed-to-lead first response plus consent-gated nurture sequences.

Nothing here weakens compliance: every touch is re-checked with `Compliance.can_contact` immediately before it is sent, an opted-out lead
never receives one, and auto-sending is a per-workspace opt-in (the default is an approval queue)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .compliance import Compliance
from .config import Settings
from .db import DB
from .models import utcnow

CHANNELS = ("whatsapp", "email", "auto")                 # "auto" = WhatsApp if the lead has a phone, else email
TRIGGERS = ("new_lead",)
MAX_STEPS, MAX_DELAY_H, MAX_TEMPLATE = 12, 2160, 1000
_PLACEHOLDER = re.compile(r"\{(name|area|budget|agent)\}")

_FIRST_EN = ("Hi {name}, thanks for your enquiry{area}. This is {agent}. I'm already looking at options for you - "
             "what budget and timeline do you have in mind?")
_FIRST_AR = "مرحباً {name}، شكراً لاستفسارك{area}. معك {agent}. بدأت أراجع الخيارات المناسبة لك - ما هي ميزانيتك والوقت الذي تفكر فيه؟"
DEFAULT_SEQUENCES: list[dict] = [
    {"name": "first_response", "trigger": "new_lead", "enabled": True, "steps": [
        {"delay_hours": 0, "channel": "auto", "template_en": _FIRST_EN, "template_ar": _FIRST_AR}]},
    {"name": "nurture_90", "trigger": "new_lead", "enabled": True, "steps": [
        {"delay_hours": d * 24, "channel": ch,
         "template_en": f"Hi {{name}}, {{agent}} here. Any update on your search{{area}}{{budget}}? I can send a short list of matching options.",
         "template_ar": "مرحباً {name}، معك {agent}. هل من جديد في بحثك{area}{budget}؟ يمكنني إرسال قائمة قصيرة بالخيارات المناسبة."}
        for d, ch in ((2, "auto"), (5, "email"), (10, "auto"), (21, "email"), (45, "auto"), (90, "email"))]},
]


class SequenceError(ValueError):
    pass


def _budget(b) -> str:
    if not b:
        return ""
    return f" (around AED {b / 1e6:.1f}M)" if b >= 1e6 else f" (around AED {b / 1e3:.0f}K)"


def render(template_en: str, template_ar: str | None, lead: dict, settings: Settings) -> str:
    """Fill placeholders. Arabic leads get the Arabic template when there is one, else the English text. Missing data never leaves braces."""
    ar = lead.get("language") == "ar"
    text = template_ar if (ar and template_ar) else template_en
    area = lead.get("primary_area")
    values = {"name": lead.get("display_name") or ("there" if not (ar and template_ar) else ""), "agent": settings.agent_name,
              "area": (f" in {area}" if not (ar and template_ar) else f" في {area}") if area else "",
              "budget": _budget(lead.get("budget_max"))}
    return re.sub(r"\s{2,}", " ", _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)).strip()


def _validate(seqs) -> list[dict]:
    if not isinstance(seqs, list) or not 1 <= len(seqs) <= 20:
        raise SequenceError("provide between 1 and 20 sequences")
    out, names = [], set()
    for s in seqs:
        if not isinstance(s, dict):
            raise SequenceError("invalid sequence")
        name, trig, steps = str(s.get("name", "")).strip()[:60], s.get("trigger"), s.get("steps")
        if not re.fullmatch(r"[A-Za-z0-9_\- ]{1,60}", name) or name in names:
            raise SequenceError("sequence names must be unique, 1-60 letters, digits, space, - or _")
        if trig not in TRIGGERS:
            raise SequenceError(f"trigger must be one of {TRIGGERS}")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise SequenceError(f"a sequence needs 1-{MAX_STEPS} steps")
        clean = []
        for st in steps:
            d, ch, en, ar = st.get("delay_hours"), st.get("channel"), st.get("template_en"), st.get("template_ar")
            if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0 <= d <= MAX_DELAY_H:
                raise SequenceError(f"delay_hours must be 0-{MAX_DELAY_H}")
            if ch not in CHANNELS:
                raise SequenceError(f"channel must be one of {CHANNELS}")
            if not isinstance(en, str) or not en.strip() or len(en) > MAX_TEMPLATE or (ar is not None and (not isinstance(ar, str) or len(ar) > MAX_TEMPLATE)):
                raise SequenceError(f"templates are required (English) and at most {MAX_TEMPLATE} characters")
            clean.append({"delay_hours": d, "channel": ch, "template_en": en.strip(), "template_ar": (ar or "").strip() or None})
        names.add(name)
        out.append({"name": name, "trigger": trig, "enabled": bool(s.get("enabled", True)), "steps": clean})
    return out


class Followups:
    def __init__(self, db: DB, compliance: Compliance, settings: Settings, wa_sender, mailer):
        self.db, self.comp, self.s, self.wa, self.mailer = db, compliance, settings, wa_sender, mailer

    # ------------------------------------------------------------------ configuration
    def ensure_defaults(self) -> None:
        if self.db.one("SELECT 1 FROM kv WHERE key='followups_seeded'"):
            return
        with self.db.tx() as c:
            for d in DEFAULT_SEQUENCES:
                c.execute("INSERT OR IGNORE INTO sequences(name,trigger,enabled,steps,created_at) VALUES(?,?,?,?,?)",
                          (d["name"], d["trigger"], 1, json.dumps(d["steps"]), utcnow().isoformat()))
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('followups_seeded','1')")

    def sequences(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"], "trigger": r["trigger"], "enabled": bool(r["enabled"]), "steps": json.loads(r["steps"])}
                for r in self.db.q("SELECT * FROM sequences ORDER BY id")]

    def put_sequences(self, seqs) -> list[dict]:
        clean = _validate(seqs)
        with self.db.tx() as c:
            keep = []
            for s in clean:
                c.execute("INSERT INTO sequences(name,trigger,enabled,steps,created_at) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(name) DO UPDATE SET trigger=excluded.trigger, enabled=excluded.enabled, steps=excluded.steps",
                          (s["name"], s["trigger"], int(s["enabled"]), json.dumps(s["steps"]), utcnow().isoformat()))
                keep.append(s["name"])
            c.execute(f"UPDATE sequences SET enabled=0 WHERE name NOT IN ({','.join('?' * len(keep))})", keep)      # removed sequences stop enrolling
        return self.sequences()

    def autosend(self) -> bool:
        r = self.db.one("SELECT value FROM kv WHERE key='followups_autosend'")
        return bool(r and r["value"] == "1")

    def set_autosend(self, on: bool) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('followups_autosend',?)", ("1" if on else "0",))

    # ------------------------------------------------------------------ enrolment
    def enroll(self, lead_id: int, trigger: str, now: datetime | None = None) -> list[int]:
        """Start every enabled sequence for this trigger that the lead is not already in. Returns the new run ids."""
        self.ensure_defaults()
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        runs: list[int] = []
        with self.db.tx() as c:
            for seq in c.execute("SELECT * FROM sequences WHERE enabled=1 AND trigger=?", (trigger,)).fetchall():
                cur = c.execute("INSERT OR IGNORE INTO sequence_runs(lead_id,sequence_id,state,started_at) VALUES(?,?,?,?)",
                                (lead_id, seq["id"], "active", now.isoformat()))
                if not cur.rowcount:
                    continue
                for i, st in enumerate(json.loads(seq["steps"])):
                    c.execute("INSERT INTO touches(run_id,lead_id,step_index,channel,due_at) VALUES(?,?,?,?,?)",
                              (cur.lastrowid, lead_id, i, st["channel"], (now + timedelta(hours=st["delay_hours"])).isoformat()))
                runs.append(cur.lastrowid)
        return runs
