"""WhatsApp Cloud API: inbound webhook parsing, a send abstraction (dry-run by default) and the reply handler.

Compliance stance: we only ever REPLY to people who messaged us first. Replies are exempt from quiet hours (the person is mid-chat) but
never from opt-out: STOP erases the lead and writes a suppression hash, and a suppressed number gets no further replies."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..models import Signal, utcnow
from ..qualify import advance, is_stop
from .base import http_json

GRAPH = "https://graph.facebook.com/v21.0"
UNSUBSCRIBED = {"en": "You've been unsubscribed and your details removed. We won't message you again.",
                "ar": "تم إلغاء اشتراكك وحذف بياناتك. لن نراسلك مرة أخرى."}


@dataclass
class Inbound:
    wa_id: str
    name: str | None
    text: str
    msg_id: str
    ts: int


class Sender(Protocol):
    def send_text(self, to: str, text: str) -> None: ...


class DryRunSender:
    """Records replies instead of sending them (the default until WHATSAPP_SEND_ENABLED and credentials are set)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class MetaSender:
    def __init__(self, settings: Settings, http=http_json):
        self.s, self.http = settings, http

    def send_text(self, to: str, text: str) -> None:
        self.http(f"{GRAPH}/{self.s.whatsapp_phone_id}/messages", method="POST",
                  headers={"Authorization": f"Bearer {self.s.whatsapp_token}"},
                  data={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})


def make_sender(settings: Settings) -> Sender:
    if settings.whatsapp_send_enabled and settings.whatsapp_token and settings.whatsapp_phone_id:
        return MetaSender(settings)
    return DryRunSender()


def parse_inbound(payload: dict) -> list[Inbound]:
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                if m.get("type") != "text":
                    continue
                out.append(Inbound(wa_id=m["from"], name=names.get(m["from"]), text=(m.get("text") or {}).get("body", ""),
                                   msg_id=m["id"], ts=int(m.get("timestamp") or 0)))
    return out


def handle_inbound(pipe, sender: Sender, inb: Inbound) -> dict:
    phone = "+" + inb.wa_id.lstrip("+")
    if is_stop(inb.text):
        erased = pipe.opt_out(phone=phone)
        sender.send_text(inb.wa_id, UNSUBSCRIBED["ar" if any("؀" <= ch <= "ۿ" for ch in inb.text) else "en"])
        return {"action": "opt_out", "leads_erased": erased}

    res = pipe.ingest(Signal(platform="whatsapp", external_id=inb.msg_id, source_type="lead_form", text=inb.text,
                             author_name=inb.name, phone=phone))
    if res.status in ("suppressed", "duplicate"):
        return {"action": res.status}                                   # silence: opted-out people and webhook retries
    lead_id = res.lead_id
    row = pipe.db.one("SELECT state, transcript FROM conversations WHERE lead_id=? AND channel='whatsapp'", (lead_id,))
    state = json.loads(row["state"]) if row else {}
    transcript = json.loads(row["transcript"]) if row else []
    transcript.append({"from": "person", "text": inb.text[:500], "ts": inb.ts})
    if state.get("step") == "done":                                     # flow finished: a human takes over, the bot stays quiet
        with pipe.db.tx() as c:
            c.execute("UPDATE conversations SET transcript=?, updated_at=? WHERE lead_id=? AND channel='whatsapp'",
                      (json.dumps(transcript[-40:]), utcnow().isoformat(), lead_id))
        return {"action": "handoff", "lead_id": lead_id}
    was_consented = state.get("answers", {}).get("consent") is not None
    state, reply, done = advance(state, inb.text)
    if reply:
        transcript.append({"from": "agency", "text": reply})
    with pipe.db.tx() as c:
        c.execute("INSERT INTO conversations(lead_id,channel,state,transcript,updated_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(lead_id,channel) DO UPDATE SET state=excluded.state, transcript=excluded.transcript, updated_at=excluded.updated_at",
                  (lead_id, "whatsapp", json.dumps(state), json.dumps(transcript[-40:]), utcnow().isoformat()))
    pipe.recompute(lead_id)                                             # conversation answers now outrank text heuristics
    if done and state["answers"].get("consent") is True and not was_consented:
        pipe.record_consent(lead_id, json.dumps({"channel": "whatsapp", "answers": state["answers"], "transcript": transcript[-12:]},
                                                ensure_ascii=False), actor="whatsapp")
    if reply:
        sender.send_text(inb.wa_id, reply)
    return {"action": "replied", "lead_id": lead_id, "done": done}
