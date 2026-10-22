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


class _Config:
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
    def enroll(self, lead_id: int, trigger: str, now: datetime | None = None, exclude: tuple = ()) -> list[int]:
        """Start every enabled sequence for this trigger that the lead is not already in. Returns the new run ids."""
        self.ensure_defaults()
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        runs: list[int] = []
        with self.db.tx() as c:
            for seq in c.execute("SELECT * FROM sequences WHERE enabled=1 AND trigger=?", (trigger,)).fetchall():
                if seq["name"] in exclude:
                    continue
                cur = c.execute("INSERT OR IGNORE INTO sequence_runs(lead_id,sequence_id,state,started_at) VALUES(?,?,?,?)",
                                (lead_id, seq["id"], "active", now.isoformat()))
                if not cur.rowcount:
                    continue
                for i, st in enumerate(json.loads(seq["steps"])):
                    c.execute("INSERT INTO touches(run_id,lead_id,step_index,channel,due_at) VALUES(?,?,?,?,?)",
                              (cur.lastrowid, lead_id, i, st["channel"], (now + timedelta(hours=st["delay_hours"])).isoformat()))
                runs.append(cur.lastrowid)
        return runs


class FollowupError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


MAX_PER_WEEK = 3
MAX_ATTEMPTS = 3
WINDOW = timedelta(hours=24)
_SENT = ("sent", "dry_run")


def _iso(d: datetime) -> str:
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse(v: str) -> datetime:
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _footer(lead: dict, s: Settings, channel: str) -> str:
    ar = lead.get("language") == "ar"
    sig = f"{s.agent_name} | {s.agency_name} | BRN {s.brn}"
    if channel == "whatsapp":
        return f"\n— {sig}\n" + ("للإيقاف أرسل: إيقاف" if ar else "Reply STOP to opt out.")
    return f"\n\n{sig}\n" + ("لإيقاف الرسائل، ردّ بكلمة: إلغاء الاشتراك" if ar else "You're receiving this because you asked us to get in touch. Reply 'unsubscribe' to stop.")


class _Mixin:
    """Worker half of Followups (kept separate only to keep the file readable)."""

    # ------------------------------------------------------------------ helpers
    def _lead(self, lead_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            return None
        d = dict(r)
        try:
            d["platforms"] = json.loads(d.get("platforms") or "[]")
        except ValueError:
            d["platforms"] = []
        return d

    def _in_window(self, lead_id: int, now: datetime) -> bool:
        r = self.db.one("SELECT MAX(ingested_at) m FROM signals WHERE lead_id=? AND platform='whatsapp'", (lead_id,))
        return bool(r and r["m"] and now - _parse(r["m"]) <= WINDOW)

    def _channel(self, lead: dict, requested: str, now: datetime) -> str:
        if requested != "auto":
            return requested
        if lead.get("phone") and self._in_window(lead["id"], now):
            return "whatsapp"
        return "email" if lead.get("email") else "whatsapp"

    def _step(self, touch) -> dict:
        run = self.db.one("SELECT sequence_id FROM sequence_runs WHERE id=?", (touch["run_id"],))
        seq = self.db.one("SELECT steps FROM sequences WHERE id=?", (run["sequence_id"],))
        return json.loads(seq["steps"])[touch["step_index"]]

    def _claim(self, touch_id: int) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE touches SET status='processing' WHERE id=? AND status='pending'", (touch_id,)).rowcount == 1

    def _set(self, touch_id: int, **cols) -> None:
        with self.db.tx() as c:
            c.execute(f"UPDATE touches SET {','.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), touch_id))

    def _sent_last_week(self, lead_id: int, now: datetime) -> list[str]:
        rows = self.db.q(f"SELECT sent_at FROM touches WHERE lead_id=? AND status IN ({','.join('?' * len(_SENT))}) AND sent_at>? ORDER BY sent_at",
                         (lead_id, *_SENT, _iso(now - timedelta(days=7))))
        return [r["sent_at"] for r in rows]

    # ------------------------------------------------------------------ sending
    def _deliver(self, touch, lead: dict, channel: str, body: str, now: datetime, actor: str | None) -> str:
        """Send one touch. Returns the final status; failures are recorded on the row (retry/backoff), never raised."""
        live = True
        try:
            if channel == "whatsapp":
                self.wa.send_text(lead["phone"].lstrip("+"), body)
                live = type(self.wa).__name__ != "DryRunSender"
            else:
                self.mailer.send(lead["email"], "Your property search" if lead.get("language") != "ar" else "بحثك العقاري", body)
                live = type(self.mailer).__name__ != "ConsoleMailer"
        except Exception as e:
            attempts = touch["attempts"] + 1
            err = f"{type(e).__name__}: {e}"[:300]
            if attempts >= MAX_ATTEMPTS:
                self._set(touch["id"], status="failed", attempts=attempts, last_error=err, body=body)
                with self.db.tx() as c:
                    c.execute("INSERT INTO activities(lead_id,ts,kind,channel,note) VALUES(?,?,?,?,?)",
                              (lead["id"], _iso(now), "followup_failed", channel, err))
                self.db.bump("followup_failed")
                return "failed"
            backoff = timedelta(minutes=5 * 4 ** (attempts - 1))
            self._set(touch["id"], status="pending", attempts=attempts, last_error=err, due_at=_iso(now + backoff))
            return "pending"
        status = "sent" if live else "dry_run"
        self._set(touch["id"], status=status, sent_at=_iso(now), body=body, channel=channel, approved_by=actor, note=None if live else "dry-run: not delivered")
        with self.db.tx() as c:
            c.execute("INSERT INTO activities(lead_id,ts,kind,channel,note) VALUES(?,?,?,?,?)",
                      (lead["id"], _iso(now), f"followup:{channel}", channel, body[:200]))
        self.db.audit(actor or "system", "followup_sent", lead["id"], channel=channel, step=touch["step_index"], status=status)
        self.db.bump("followup_sent" if live else "followup_dry_run")
        return status

    def _compose(self, touch, lead: dict, channel: str) -> str:
        st = self._step(touch)
        return render(st["template_en"], st.get("template_ar"), lead, self.s) + _footer(lead, self.s, channel)

    # ------------------------------------------------------------------ worker
    def tick(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        out = {"sent": 0, "queued": 0, "skipped": 0, "deferred": 0, "failed": 0}
        auto = self.autosend()
        due = self.db.q("SELECT t.id FROM touches t JOIN sequence_runs r ON r.id=t.run_id WHERE t.status='pending' AND r.state='active' "
                        "AND t.due_at<=? ORDER BY t.due_at, t.id LIMIT 500", (_iso(now),))
        for row in due:
            if not self._claim(row["id"]):
                continue
            touch = self.db.one("SELECT * FROM touches WHERE id=?", (row["id"],))
            try:
                self._process(touch, now, auto, out)
            except Exception as e:                                      # never leave a claimed row stuck in 'processing'
                self._set(touch["id"], status="pending", last_error=f"{type(e).__name__}: {e}"[:300], due_at=_iso(now + timedelta(hours=1)))
                out["deferred"] += 1
        return out

    def _process(self, touch, now: datetime, auto: bool, out: dict) -> None:
        lead = self._lead(touch["lead_id"])
        if lead is None:
            self._set(touch["id"], status="cancelled", note="lead erased")
            return
        channel = self._channel(lead, touch["channel"], now)
        verdict = self.comp.can_contact(lead, channel, now)
        if not verdict.allowed:
            if verdict.retry_after:
                self._set(touch["id"], status="pending", due_at=_iso(verdict.retry_after), note=verdict.reason)
                out["deferred"] += 1
            else:
                self._set(touch["id"], status="skipped", note=verdict.reason)
                out["skipped"] += 1
            return
        recent = self._sent_last_week(lead["id"], now)
        if len(recent) >= MAX_PER_WEEK:
            self._set(touch["id"], status="pending", due_at=_iso(_parse(recent[len(recent) - MAX_PER_WEEK]) + timedelta(days=7)), note="weekly frequency cap")
            out["deferred"] += 1
            return
        body = self._compose(touch, lead, channel)
        if channel == "whatsapp" and not self._in_window(lead["id"], now):
            self._set(touch["id"], status="queued_for_approval", channel=channel, body=body,
                      note="Outside the 24-hour WhatsApp window: needs an approved template message. Approve to send, or skip.")
            out["queued"] += 1
            return
        if not auto:
            self._set(touch["id"], status="queued_for_approval", channel=channel, body=body, note=None)
            out["queued"] += 1
            return
        final = self._deliver(touch, lead, channel, body, now, None)
        out["sent" if final in _SENT else "failed" if final == "failed" else "deferred"] += 1

    # ------------------------------------------------------------------ human actions
    def queue(self, agent_id: int | None = None) -> list[dict]:
        sql = ("SELECT t.id, t.lead_id, t.channel, t.due_at, t.body, t.note, l.display_name, l.tier, l.assigned_agent_id FROM touches t "
               "JOIN leads l ON l.id=t.lead_id WHERE t.status='queued_for_approval'")
        args: list = []
        if agent_id is not None:
            sql += " AND l.assigned_agent_id=?"
            args.append(agent_id)
        return [dict(r) for r in self.db.q(sql + " ORDER BY t.due_at, t.id LIMIT 200", args)]

    def approve(self, touch_id: int, actor: str, body: str | None = None, now: datetime | None = None) -> dict:
        now = now or utcnow()
        touch = self.db.one("SELECT * FROM touches WHERE id=?", (touch_id,))
        if not touch or touch["status"] != "queued_for_approval":
            raise FollowupError("touch is not waiting for approval")
        with self.db.tx() as c:                                         # claim so a double click cannot send twice
            if c.execute("UPDATE touches SET status='processing' WHERE id=? AND status='queued_for_approval'", (touch_id,)).rowcount != 1:
                raise FollowupError("touch is not waiting for approval")
        lead = self._lead(touch["lead_id"])
        try:
            if lead is None:
                self._set(touch_id, status="cancelled", note="lead erased")
                raise FollowupError("lead no longer exists", 404)
            channel = touch["channel"]
            verdict = self.comp.can_contact(lead, channel, now)
            if not verdict.allowed:
                self._set(touch_id, status="queued_for_approval")
                raise FollowupError("not allowed: " + verdict.reason)
            text = (body.strip() if isinstance(body, str) and body.strip() else touch["body"] or self._compose(touch, lead, channel))[:2000]
            final = self._deliver(touch, lead, channel, text, now, actor)
        except FollowupError:
            raise
        except Exception:
            self._set(touch_id, status="queued_for_approval")
            raise
        if final == "pending":
            self._set(touch_id, status="queued_for_approval")
            raise FollowupError("sending failed; it stays in the queue")
        return {"status": final}

    def skip(self, touch_id: int, actor: str) -> None:
        with self.db.tx() as c:
            n = c.execute("UPDATE touches SET status='skipped', approved_by=?, note='skipped by a person' WHERE id=? AND status='queued_for_approval'",
                          (actor, touch_id)).rowcount
        if not n:
            raise FollowupError("touch is not waiting for approval")

    # ------------------------------------------------------------------ stop rules
    def stop(self, lead_id: int, reason: str) -> int:
        with self.db.tx() as c:
            runs = c.execute("UPDATE sequence_runs SET state='stopped', stopped_at=?, stop_reason=? WHERE lead_id=? AND state='active'",
                             (_iso(utcnow()), reason, lead_id)).rowcount
            c.execute("UPDATE touches SET status='cancelled', note=? WHERE lead_id=? AND status IN ('pending','queued_for_approval')",
                      (f"stopped: {reason}", lead_id))
        return runs

    def stop_for_phone(self, phone: str, reason: str) -> int:
        digits = phone.lstrip("+")
        return sum(self.stop(r["id"], reason) for r in self.db.q("SELECT id FROM leads WHERE phone IN (?,?)", (digits, "+" + digits)))


class Followups(_Config, _Mixin):
    """Sequences + enrolment (`_Config`) and the worker, approval queue and stop rules (`_Mixin`)."""
