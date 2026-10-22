"""Follow-up engine: speed-to-lead first response plus consent-gated nurture sequences.

Nothing here weakens compliance: every touch is re-checked with `Compliance.can_contact` immediately before it is sent, an opted-out lead
never receives one, and auto-sending is a per-workspace opt-in (the default is an approval queue)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from .compliance import GST, Compliance, Decision
from .config import Settings
from .db import DB
from .inventory import match as match_projects
from .models import utcnow

CONSENT_LEVELS = ("enquiry", "opted_in")                 # enquiry: answering their request only; opted_in: marketing / nurture
CHANNELS = ("whatsapp", "email", "auto")                 # "auto" = WhatsApp if the lead has a phone, else email
TRIGGERS = ("new_lead",)
MAX_STEPS, MAX_DELAY_H, MAX_TEMPLATE = 12, 2160, 1000
_PLACEHOLDER = re.compile(r"\{(name|area|budget|agent)\}")

_FIRST_EN = ("Hi {name}, thanks for your enquiry{area}. This is {agent}. I'm already looking at options for you - "
             "what budget and timeline do you have in mind?")
_FIRST_AR = "مرحباً {name}، شكراً لاستفسارك{area}. معك {agent}. بدأت أراجع الخيارات المناسبة لك - ما هي ميزانيتك والوقت الذي تفكر فيه؟"
DEFAULT_SEQUENCES: list[dict] = [
    {"name": "first_response", "trigger": "new_lead", "enabled": True, "min_consent": "enquiry", "steps": [
        {"delay_hours": 0, "channel": "auto", "template_en": _FIRST_EN, "template_ar": _FIRST_AR}]},
    {"name": "nurture_90", "trigger": "new_lead", "enabled": True, "min_consent": "opted_in", "steps": [
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
    out = re.sub(r"\s{2,}", " ", _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)).strip()
    return re.sub(r"\s+([،,])", r"\1", out)


def _validate(seqs) -> list[dict]:
    if not isinstance(seqs, list) or not 1 <= len(seqs) <= 20:
        raise SequenceError("provide between 1 and 20 sequences")
    out, names = [], set()
    for s in seqs:
        if not isinstance(s, dict):
            raise SequenceError("invalid sequence")
        name, trig, steps = str(s.get("name", "")).strip()[:60], s.get("trigger"), s.get("steps")
        if not isinstance(s.get("enabled", True), bool) or s.get("min_consent", "opted_in") not in CONSENT_LEVELS:
            raise SequenceError("enabled must be true or false and min_consent one of " + ", ".join(CONSENT_LEVELS))
        if not re.fullmatch(r"[A-Za-z0-9_\- ]{1,60}", name) or name in names:
            raise SequenceError("sequence names must be unique, 1-60 letters, digits, space, - or _")
        if trig not in TRIGGERS:
            raise SequenceError(f"trigger must be one of {TRIGGERS}")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise SequenceError(f"a sequence needs 1-{MAX_STEPS} steps")
        clean = []
        for st in steps:
            if not isinstance(st, dict):
                raise SequenceError("invalid step")
            d, ch, en, ar = st.get("delay_hours"), st.get("channel"), st.get("template_en"), st.get("template_ar")
            if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0 <= d <= MAX_DELAY_H:
                raise SequenceError(f"delay_hours must be 0-{MAX_DELAY_H}")
            if ch not in CHANNELS:
                raise SequenceError(f"channel must be one of {CHANNELS}")
            if not isinstance(en, str) or not en.strip() or len(en) > MAX_TEMPLATE or (ar is not None and (not isinstance(ar, str) or len(ar) > MAX_TEMPLATE)):
                raise SequenceError(f"templates are required (English) and at most {MAX_TEMPLATE} characters")
            clean.append({"delay_hours": d, "channel": ch, "template_en": en.strip(), "template_ar": (ar or "").strip() or None})
        names.add(name)
        out.append({"name": name, "trigger": trig, "enabled": bool(s.get("enabled", True)), "min_consent": s.get("min_consent", "opted_in"), "steps": clean})
    return out


def _consent_ok(status: str | None, needed: str) -> bool:
    """An enquiry may be answered; nurture and offers need marketing opt-in."""
    return status == "opted_in" or (needed == "enquiry" and status == "enquiry")


class _Config:
    def __init__(self, db: DB, compliance: Compliance, settings: Settings, wa_sender, mailer):
        self.db, self.comp, self.s, self.wa, self.mailer = db, compliance, settings, wa_sender, mailer

    # ------------------------------------------------------------------ configuration
    def ensure_defaults(self) -> None:
        if self.db.one("SELECT 1 FROM kv WHERE key='followups_seeded'"):
            return
        with self.db.tx() as c:
            for d in DEFAULT_SEQUENCES:
                c.execute("INSERT OR IGNORE INTO sequences(name,trigger,enabled,min_consent,steps,created_at) VALUES(?,?,?,?,?,?)",
                          (d["name"], d["trigger"], 1, d["min_consent"], json.dumps(d["steps"]), utcnow().isoformat()))
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('followups_seeded','1')")

    def sequences(self) -> list[dict]:
        self.ensure_defaults()
        return [{"id": r["id"], "name": r["name"], "trigger": r["trigger"], "enabled": bool(r["enabled"]), "min_consent": r["min_consent"], "steps": json.loads(r["steps"])}
                for r in self.db.q("SELECT * FROM sequences WHERE trigger!='project_alert' ORDER BY id")]

    def put_sequences(self, seqs) -> list[dict]:
        clean = _validate(seqs)
        with self.db.tx() as c:
            keep = []
            for s in clean:
                c.execute("INSERT INTO sequences(name,trigger,enabled,min_consent,steps,created_at) VALUES(?,?,?,?,?,?) "
                          "ON CONFLICT(name) DO UPDATE SET trigger=excluded.trigger, enabled=excluded.enabled, min_consent=excluded.min_consent, steps=excluded.steps",
                          (s["name"], s["trigger"], int(s["enabled"]), s["min_consent"], json.dumps(s["steps"]), utcnow().isoformat()))
                c.execute("UPDATE touches SET status='skipped', note='step removed from the sequence' WHERE status IN ('pending','queued_for_approval') AND "
                          "step_index>=? AND run_id IN (SELECT r.id FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id WHERE q.name=?)",
                          (len(s["steps"]), s["name"]))
                keep.append(s["name"])
            c.execute(f"UPDATE sequences SET enabled=0 WHERE trigger!='project_alert' AND name NOT IN ({','.join('?' * len(keep))})", keep)      # removed sequences stop
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
        lead = self.db.one("SELECT consent_status, opted_out, status FROM leads WHERE id=?", (lead_id,))
        if not lead or lead["opted_out"] or self._paused(lead_id) or lead["status"] in ("won", "lost"):
            return runs
        with self.db.tx() as c:
            for seq in c.execute("SELECT * FROM sequences WHERE enabled=1 AND trigger=?", (trigger,)).fetchall():
                if seq["name"] in exclude or not _consent_ok(lead["consent_status"], seq["min_consent"]):
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
STALE = timedelta(minutes=30)
_SENT = ("sent", "dry_run")


def _iso(d: datetime) -> str:
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse(v: str) -> datetime:
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


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

    def _paused(self, lead_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM kv WHERE key=?", (f"paused:{lead_id}",)))

    def _in_window(self, lead_id: int, now: datetime) -> bool:
        r = self.db.one("SELECT MAX(ingested_at) m FROM signals WHERE lead_id=? AND platform='whatsapp'", (lead_id,))
        return bool(r and r["m"] and now - _parse(r["m"]) <= WINDOW)

    def _channel(self, lead: dict, requested: str, now: datetime) -> str:
        if requested != "auto":
            return requested
        if lead.get("phone") and self._in_window(lead["id"], now):
            return "whatsapp"
        return "email" if lead.get("email") else "whatsapp"

    def _step(self, touch) -> dict | None:
        row = self.db.one("SELECT q.steps FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id WHERE r.id=?", (touch["run_id"],))
        steps = json.loads(row["steps"]) if row else []
        return steps[touch["step_index"]] if touch["step_index"] < len(steps) else None

    def _min_consent(self, touch) -> str:
        row = self.db.one("SELECT q.min_consent FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id WHERE r.id=?", (touch["run_id"],))
        return row["min_consent"] if row else "opted_in"

    def _claim(self, touch_id: int, now: datetime | None = None) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE touches SET status='processing', claimed_at=? WHERE id=? AND status='pending'",
                             (_iso(now or utcnow()), touch_id)).rowcount == 1

    def _set(self, touch_id: int, expect: tuple = ("processing",), **cols) -> bool:
        """Conditional write: a stop/cancel that happened meanwhile is never overwritten."""
        with self.db.tx() as c:
            return c.execute(f"UPDATE touches SET {','.join(k + '=?' for k in cols)} WHERE id=? AND status IN ({','.join('?' * len(expect))})",
                             (*cols.values(), touch_id, *expect)).rowcount == 1

    def _run_active(self, touch) -> bool:
        r = self.db.one("SELECT state FROM sequence_runs WHERE id=?", (touch["run_id"],))
        return bool(r and r["state"] == "active")

    def _sent_last_week(self, lead_id: int, now: datetime) -> list[str]:
        rows = self.db.q(f"SELECT sent_at FROM touches WHERE lead_id=? AND status IN ({','.join('?' * len(_SENT))}) AND sent_at>? ORDER BY sent_at",
                         (lead_id, *_SENT, _iso(now - timedelta(days=7))))
        return [r["sent_at"] for r in rows]

    # ------------------------------------------------------------------ unsubscribe links
    def _secret(self) -> bytes:
        r = self.db.one("SELECT value FROM kv WHERE key='unsub_secret'")
        if r:
            return r["value"].encode()
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('unsub_secret',?)", (secrets.token_hex(32),))
        return self.db.one("SELECT value FROM kv WHERE key='unsub_secret'")["value"].encode()

    def unsubscribe_url(self, lead_id: int) -> str:
        base = (self.s.public_url or self.s.base_url or "").rstrip("/")
        mac = hmac.new(self._secret(), f"unsub:{lead_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{base}/unsubscribe?t={lead_id}.{mac}"

    def verify_unsubscribe(self, token: str) -> int | None:
        m = re.fullmatch(r"(\d{1,12})\.([0-9a-f]{32})", token or "")
        if not m:
            return None
        want = hmac.new(self._secret(), f"unsub:{m.group(1)}".encode(), hashlib.sha256).hexdigest()[:32]
        return int(m.group(1)) if hmac.compare_digest(want, m.group(2)) else None

    def _footer(self, lead: dict, channel: str) -> str:
        ar = lead.get("language") == "ar"
        sig = f"{self.s.agent_name} | {self.s.agency_name} | BRN {self.s.brn}"
        if channel == "whatsapp":
            return f"\n— {sig}\n" + ("للإيقاف أرسل: إيقاف" if ar else "Reply STOP to opt out.")
        link = self.unsubscribe_url(lead["id"])
        return f"\n\n{sig}\n" + ("لإيقاف الرسائل: " if ar else "You're receiving this because you asked us to get in touch. Unsubscribe: ") + link

    # ------------------------------------------------------------------ gate
    def _gate(self, lead: dict, channel: str, now: datetime, min_consent: str) -> Decision:
        """Compliance verdict plus the rules the generic gate leaves to its callers: DNC covers WhatsApp too, e-mail respects contact hours,
        and the consent level the sequence needs (nurture needs marketing opt-in, an enquiry only entitles a direct answer)."""
        if not _consent_ok(lead.get("consent_status"), min_consent):
            return Decision(False, "This sequence needs marketing opt-in; the person only made an enquiry.")
        v = self.comp.can_contact(lead, channel, now)
        if not v.allowed:
            return v
        if channel == "whatsapp" and lead.get("phone") in self.comp.dnc():
            return Decision(False, "Number is on the Do-Not-Call list.")
        if channel == "email":
            local = now.astimezone(GST)
            if not (self.s.contact_hour_start <= local.hour < self.s.contact_hour_end):
                nxt = local.replace(hour=self.s.contact_hour_start, minute=0, second=0, microsecond=0)
                if local.hour >= self.s.contact_hour_end:
                    nxt += timedelta(days=1)
                return Decision(False, f"Outside permitted contact hours {self.s.contact_hour_start:02d}:00-{self.s.contact_hour_end:02d}:00 UAE time.", nxt)
        return v

    # ------------------------------------------------------------------ sending
    def _deliver(self, touch, lead: dict, channel: str, body: str, now: datetime, actor: str | None) -> str:
        """Send one touch. Returns the final status; failures are recorded on the row (retry/backoff), never raised."""
        cur = self.db.one("SELECT status FROM touches WHERE id=?", (touch["id"],))
        if not cur or cur["status"] != "processing" or not self._run_active(touch):
            return "cancelled"                                          # stopped while we held it: nothing leaves the building
        live = True
        try:
            if channel == "whatsapp":
                self.wa.send_text(lead["phone"].lstrip("+"), body)
                live = type(self.wa).__name__ != "DryRunSender"
            else:
                self.mailer.send(lead["email"], "Your property search" if lead.get("language") != "ar" else "بحثك العقاري", body,
                                 headers={"List-Unsubscribe": f"<{self.unsubscribe_url(lead['id'])}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
                live = type(self.mailer).__name__ != "ConsoleMailer"
        except Exception as e:
            attempts = touch["attempts"] + 1
            err = f"{type(e).__name__}: {e}"[:300]
            if attempts >= MAX_ATTEMPTS:
                self._set(touch["id"], status="failed", attempts=attempts, last_error=err, body=body)
                self._record(lead["id"], "followup_failed", channel, err, now)
                return "failed"
            self._set(touch["id"], status="pending", attempts=attempts, last_error=err, due_at=_iso(now + timedelta(minutes=5 * 4 ** (attempts - 1))))
            return "pending"
        status = "sent" if live else "dry_run"
        self._set(touch["id"], status=status, sent_at=_iso(now), body=body, channel=channel, approved_by=actor, note=None if live else "dry-run: not delivered")
        # bookkeeping happens after the status is final and can never undo it (a failure here must not cause a second send)
        self._record(lead["id"], f"followup:{channel}", channel, body[:200], now)
        try:
            self.db.audit(actor or "system", "followup_sent", lead["id"], channel=channel, step=touch["step_index"], status=status)
            self.db.bump("followup_sent" if live else "followup_dry_run")
        except Exception:
            pass
        return status

    def _record(self, lead_id: int, kind: str, channel: str, note: str, now: datetime) -> None:
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO activities(lead_id,ts,kind,channel,note) VALUES(?,?,?,?,?)", (lead_id, _iso(now), kind, channel, note))
            if kind == "followup_failed":
                self.db.bump("followup_failed")
        except Exception:
            pass

    def _compose(self, touch, lead: dict, channel: str) -> str:
        st = self._step(touch)
        return render(st["template_en"], st.get("template_ar"), lead, self.s) + self._footer(lead, channel)

    # ------------------------------------------------------------------ worker
    def tick(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        out = {"sent": 0, "queued": 0, "skipped": 0, "deferred": 0, "failed": 0}
        with self.db.tx() as c:                                         # a worker that died mid-send: never auto-resend (it may have gone out)
            c.execute("UPDATE touches SET status='failed', note='worker stopped mid-send: check before resending' "
                      "WHERE status='processing' AND claimed_at<?", (_iso(now - STALE),))
        auto = self.autosend()
        due = self.db.q("SELECT t.id FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id "
                        "WHERE t.status='pending' AND r.state='active' AND (q.enabled=1 OR q.trigger='project_alert') AND t.due_at<=? "
                        "ORDER BY t.due_at, t.id LIMIT 500", (_iso(now),))
        for row in due:
            if not self._claim(row["id"], now):
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
        if self._paused(lead["id"]) or lead["status"] in ("won", "lost"):
            self._set(touch["id"], status="cancelled", note="lead paused or closed")
            return
        step = self._step(touch)
        if step is None:
            self._set(touch["id"], status="skipped", note="step removed from the sequence")
            out["skipped"] += 1
            return
        channel = self._channel(lead, touch["channel"], now)
        verdict = self._gate(lead, channel, now, self._min_consent(touch))
        if not verdict.allowed:
            if verdict.retry_after:
                self._set(touch["id"], status="pending", due_at=_iso(verdict.retry_after), note=verdict.reason)
                out["deferred"] += 1
            else:
                self._set(touch["id"], status="skipped", note=verdict.reason)
                out["skipped"] += 1
            return
        recent = self._sent_last_week(lead["id"], now)
        waiting = self.db.one("SELECT COUNT(*) n FROM touches WHERE lead_id=? AND status='queued_for_approval'", (lead["id"],))["n"]
        if len(recent) + waiting >= MAX_PER_WEEK:
            when = _parse(recent[len(recent) + waiting - MAX_PER_WEEK]) + timedelta(days=7) if len(recent) + waiting - MAX_PER_WEEK < len(recent) else now + timedelta(days=1)
            self._set(touch["id"], status="pending", due_at=_iso(max(when, now + timedelta(hours=1))), note="weekly frequency cap")
            out["deferred"] += 1
            return
        if channel == "whatsapp" and not self._in_window(lead["id"], now):
            self._set(touch["id"], status="skipped", channel=channel,
                      note="WhatsApp only allows free text within 24 hours of the person's last message and message templates are not set up yet.")
            out["skipped"] += 1
            return
        body = self._compose(touch, lead, channel)
        if not auto:
            self._set(touch["id"], status="queued_for_approval", channel=channel, body=body, note=None)
            out["queued"] += 1
            return
        final = self._deliver(touch, lead, channel, body, now, None)
        out["sent" if final in _SENT else "failed" if final == "failed" else "deferred"] += 1

    # ------------------------------------------------------------------ human actions
    def queue(self, agent_id: int | None = None) -> list[dict]:
        sql = ("SELECT t.id, t.lead_id, t.channel, t.due_at, t.body, t.note, l.display_name, l.tier, l.assigned_agent_id FROM touches t "
               "JOIN leads l ON l.id=t.lead_id JOIN sequence_runs r ON r.id=t.run_id WHERE t.status='queued_for_approval' AND r.state='active'")
        args: list = []
        if agent_id is not None:
            sql += " AND l.assigned_agent_id=?"
            args.append(agent_id)
        return [dict(r) for r in self.db.q(sql + " ORDER BY t.due_at, t.id LIMIT 200", args)]

    def approve(self, touch_id: int, actor: str, body: str | None = None, now: datetime | None = None) -> dict:
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        touch = self.db.one("SELECT * FROM touches WHERE id=?", (touch_id,))
        if not touch or touch["status"] != "queued_for_approval" or not self._run_active(touch):
            raise FollowupError("touch is not waiting for approval")
        with self.db.tx() as c:                                         # claim so a double click cannot send twice
            if c.execute("UPDATE touches SET status='processing', claimed_at=? WHERE id=? AND status='queued_for_approval'", (_iso(now), touch_id)).rowcount != 1:
                raise FollowupError("touch is not waiting for approval")

        def back(msg: str, code: int = 409):
            self._set(touch_id, status="queued_for_approval")
            raise FollowupError(msg, code)

        lead = self._lead(touch["lead_id"])
        if lead is None:
            self._set(touch_id, status="cancelled", note="lead erased")
            raise FollowupError("lead no longer exists", 404)
        try:
            channel = touch["channel"]
            verdict = self._gate(lead, channel, now, self._min_consent(touch))
            if not verdict.allowed:
                back("not allowed: " + verdict.reason)
            if channel == "whatsapp" and not self._in_window(lead["id"], now):
                back("WhatsApp free text is only allowed within 24 hours of the person's last message; skip this one or contact them another way")
            if len(self._sent_last_week(lead["id"], now)) >= MAX_PER_WEEK:
                back("weekly frequency cap reached for this person")
            text = (body.strip() if isinstance(body, str) and body.strip() else touch["body"] or self._compose(touch, lead, channel))[:2000]
            footer = self._footer(lead, channel).strip()
            if footer not in text:                                       # an edit can never remove the disclosure and the opt-out
                text = text.rstrip() + "\n" + footer
            final = self._deliver(touch, lead, channel, text, now, actor)
        except FollowupError:
            raise
        except Exception:
            self._set(touch_id, status="queued_for_approval")
            raise
        if final == "pending":
            back("sending failed; it stays in the queue")
        if final == "cancelled":
            raise FollowupError("the sequence was stopped")
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
            c.execute("UPDATE touches SET status='cancelled', note=? WHERE lead_id=? AND status IN ('pending','processing','queued_for_approval')",
                      (f"stopped: {reason}", lead_id))
            if reason in ("paused", "closed"):                          # remembered, so later enrolments and offers respect it
                c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"paused:{lead_id}", reason))
        return runs

    def stop_if_replied(self, lead_id: int) -> int:
        """A message from the person ends their sequences, but only once we have actually written to them: the chat that created the lead
        (the qualification bot, the enquiry form) is not a reply to a follow-up."""
        if not self.db.one(f"SELECT 1 FROM touches WHERE lead_id=? AND status IN ({','.join('?' * len(_SENT))}) LIMIT 1", (lead_id, *_SENT)):
            return 0
        return self.stop(lead_id, "replied")

    def stop_for_phone(self, phone: str, reason: str = "replied") -> int:
        digits = phone.lstrip("+")
        ids = [r["id"] for r in self.db.q("SELECT id FROM leads WHERE phone IN (?,?)", (digits, "+" + digits))]
        return sum(self.stop_if_replied(i) if reason == "replied" else self.stop(i, reason) for i in ids)


class _Insights:
    """Speed-to-lead metrics and new-project alerts."""

    def stats(self) -> dict:
        from statistics import median
        from .scoring import SLA_MINUTES
        gaps, within = [], []
        rows = self.db.q(f"SELECT l.first_seen, l.tier, MIN(t.sent_at) sent FROM touches t JOIN leads l ON l.id=t.lead_id "
                         f"JOIN sequence_runs sr ON sr.id=t.run_id JOIN sequences q ON q.id=sr.sequence_id "
                         f"WHERE q.name='first_response' AND t.status IN ({','.join('?' * len(_SENT))}) AND l.first_seen IS NOT NULL GROUP BY t.lead_id", _SENT)
        for r in rows:
            mins = max(0.0, (_parse(r["sent"]) - _parse(r["first_seen"])).total_seconds() / 60)
            gaps.append(mins)
            if r["tier"] in SLA_MINUTES:
                within.append(mins <= SLA_MINUTES[r["tier"]])
        per_step = []
        for r in self.db.q(f"SELECT q.name sequence, t.step_index step, COUNT(*) sent FROM touches t JOIN sequence_runs sr ON sr.id=t.run_id "
                           f"JOIN sequences q ON q.id=sr.sequence_id WHERE t.status IN ({','.join('?' * len(_SENT))}) AND q.trigger!='project_alert' "
                           f"GROUP BY q.name, t.step_index ORDER BY q.id, t.step_index", _SENT):
            replied = self.db.one(
                f"SELECT COUNT(*) n FROM sequence_runs sr JOIN sequences q ON q.id=sr.sequence_id WHERE q.name=? AND sr.stop_reason='replied' AND "
                f"(SELECT MAX(step_index) FROM touches WHERE run_id=sr.id AND status IN ({','.join('?' * len(_SENT))}))=?", (r["sequence"], *_SENT, r["step"]))["n"]
            per_step.append({**dict(r), "replied": replied})
        count = lambda st: self.db.one("SELECT COUNT(*) n FROM touches WHERE status=?", (st,))["n"]      # noqa: E731
        return {"first_response_median_minutes": round(median(gaps), 1) if gaps else None,
                "within_sla_pct": round(100 * sum(within) / len(within)) if within else None,
                "queued": count("queued_for_approval"), "failed": count("failed"), "per_step": per_step}

    def alert_new_project(self, project_id: int, now: datetime | None = None, golden_min: float = 2_000_000) -> list[int]:
        """Offer a newly imported project to consented leads whose area (hard) and budget fit. One alert per lead per week."""
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        p = self.db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not p:
            return []
        clean = lambda v: re.sub(r"[{}]", "", str(v or ""))                                             # noqa: E731
        price = f"AED {p['min_price'] / 1e6:.1f}M" if p["min_price"] >= 1e6 else f"AED {p['min_price'] / 1e3:.0f}K"
        text = (f"Hi {{name}}, {{agent}} here. A new project just launched in {clean(p['area'])}: {clean(p['name'])}"
                f"{' by ' + clean(p['developer']) if p['developer'] else ''}, from {price}. Want the brochure and payment plan?")
        name, alerted = f"alert:{project_id}", []
        week_ago = _iso(now - timedelta(days=7))
        for lead in self.db.q("SELECT * FROM leads WHERE consent_status='opted_in' AND opted_out=0 AND tier!='DISCARD' AND status NOT IN ('won','lost')"):
            d = dict(lead)
            if self._paused(d["id"]):
                continue
            hit = next((m for m in match_projects(self.db, d, golden_min, limit=200, now=now.replace(tzinfo=None)) if m["project"] == p["name"] and m["developer"] == p["developer"]), None)
            if not hit or (d.get("budget_max") and not any(r.startswith("budget") for r in hit["reasons"])):
                continue
            if self.db.one("SELECT 1 FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id WHERE r.lead_id=? AND q.trigger='project_alert' AND r.started_at>?",
                           (d["id"], week_ago)):
                continue
            with self.db.tx() as c:
                c.execute("INSERT OR IGNORE INTO sequences(name,trigger,enabled,steps,created_at) VALUES(?,?,?,?,?)",
                          (name, "project_alert", 0, json.dumps([{"delay_hours": 0, "channel": "auto", "template_en": text, "template_ar": None}]), _iso(now)))
                sid = c.execute("SELECT id FROM sequences WHERE name=?", (name,)).fetchone()["id"]
                cur = c.execute("INSERT OR IGNORE INTO sequence_runs(lead_id,sequence_id,state,started_at) VALUES(?,?,?,?)", (d["id"], sid, "active", _iso(now)))
                if cur.rowcount:
                    c.execute("INSERT INTO touches(run_id,lead_id,step_index,channel,due_at) VALUES(?,?,?,?,?)", (cur.lastrowid, d["id"], 0, "auto", _iso(now)))
                    alerted.append(d["id"])
        return alerted


class Followups(_Config, _Mixin, _Insights):
    """Sequences + enrolment (`_Config`), the worker, approval queue and stop rules (`_Mixin`), metrics and alerts (`_Insights`)."""
