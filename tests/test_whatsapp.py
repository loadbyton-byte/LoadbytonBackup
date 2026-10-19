import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.connectors.whatsapp import DryRunSender, Inbound, MetaSender, handle_inbound, make_sender, parse_inbound
from leadengine.db import DB


def inb(text, n=[0], wa="971501234567", name="Aisha"):
    n[0] += 1
    return Inbound(wa_id=wa, name=name, text=text, msg_id=f"m{n[0]}-{text[:6]}", ts=1_700_000_000 + n[0])


def test_parse_inbound_text_only():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Aisha"}, "wa_id": "971501234567"}],
        "messages": [{"from": "971501234567", "id": "wamid.1", "timestamp": "1700000000", "type": "text", "text": {"body": "Hi"}},
                     {"from": "971501234567", "id": "wamid.2", "timestamp": "1700000001", "type": "image"}],
        "statuses": [{"id": "x"}]}}]}]}
    [m] = parse_inbound(payload)
    assert (m.wa_id, m.name, m.text, m.msg_id) == ("971501234567", "Aisha", "Hi", "wamid.1")


def test_full_conversation_creates_enquiry_then_optin_with_transcript(pipe):
    s = DryRunSender()
    for t in ["Hi, I want to buy an apartment", "Dubai Marina", "1.5M", "next month"]:
        handle_inbound(pipe, s, inb(t))
    lead = pipe.db.one("SELECT * FROM leads")
    assert lead["consent_status"] == "enquiry" and lead["phone"] == "+971501234567" and lead["primary_area"] == "Dubai Marina"
    assert "YES" in s.sent[-1][1]
    handle_inbound(pipe, s, inb("yes"))
    lead = pipe.db.one("SELECT * FROM leads")
    ev = json.loads(lead["consent_evidence"])
    assert lead["consent_status"] == "opted_in" and "yes" in ev["text"].lower() and "Dubai Marina" in ev["text"]


@pytest.mark.parametrize("stop", ["STOP", "Stop.", "ايقاف", "إيقاف", "وقف"])
@pytest.mark.parametrize("wa", ["971501234567", "447700900123"])
def test_stop_variants_opt_out_and_go_silent(pipe, stop, wa):
    s = DryRunSender()
    handle_inbound(pipe, s, inb("I want to buy a villa in Dubai Hills", wa=wa))
    sent_before = len(s.sent)
    res = handle_inbound(pipe, s, inb(stop, wa=wa))
    assert res["action"] == "opt_out" and pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 0
    assert pipe.db.one("SELECT COUNT(*) n FROM suppression")["n"] >= 1
    assert len(s.sent) == sent_before + 1 and "?" not in s.sent[-1][1]          # one confirmation, no qualification question
    assert handle_inbound(pipe, s, inb("hello again", wa=wa))["action"] == "suppressed"
    assert len(s.sent) == sent_before + 1                                       # silence after opt-out


def test_duplicate_webhook_delivery_does_not_double_reply(pipe):
    s = DryRunSender()
    m = inb("I want to buy an apartment")
    handle_inbound(pipe, s, m)
    assert handle_inbound(pipe, s, m)["action"] == "duplicate" and len(s.sent) == 1


def test_dry_run_is_default_and_meta_sender_only_when_enabled():
    assert isinstance(make_sender(Settings(db_path=":memory:")), DryRunSender)
    assert isinstance(make_sender(Settings(db_path=":memory:", whatsapp_send_enabled=True)), DryRunSender)      # no creds
    calls = []
    st = Settings(db_path=":memory:", whatsapp_send_enabled=True, whatsapp_token="T", whatsapp_phone_id="PID")
    assert isinstance(make_sender(st), MetaSender)
    MetaSender(st, http=lambda url, **kw: calls.append((url, kw)) or {}).send_text("971501234567", "hello")
    url, kw = calls[0]
    assert "PID/messages" in url and kw["headers"]["Authorization"] == "Bearer T" and kw["data"]["to"] == "971501234567"


def test_webhook_verify_signature_and_end_to_end():
    s = Settings(db_path=":memory:", api_key="", meta_app_secret="sec", meta_verify_token="vt")
    sender = DryRunSender()
    c = TestClient(create_app(s, DB(":memory:"), whatsapp_sender=sender))
    assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "7"}).text == "7"
    assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "7"}).status_code == 403
    body = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"profile": {"name": "A"}, "wa_id": "971501234567"}],
          "messages": [{"from": "971501234567", "id": "w1", "timestamp": "1700000000", "type": "text",
                        "text": {"body": "I want to buy an apartment in JVC"}}]}}]}]}).encode()
    assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert len(sender.sent) == 1 and "budget" in sender.sent[0][1].lower()
    assert c.get("/leads").json()[0]["consent_status"] == "enquiry"


def test_after_flow_completes_further_messages_hand_off_silently(pipe):
    s = DryRunSender()
    for t in ["Hi, I want to buy an apartment", "Dubai Marina", "1.5M", "next month", "yes"]:
        handle_inbound(pipe, s, inb(t))
    n = len(s.sent)
    res = handle_inbound(pipe, s, inb("Can we view it on Friday?"))
    assert res["action"] == "handoff" and len(s.sent) == n
    transcript = json.loads(pipe.db.one("SELECT transcript FROM conversations")["transcript"])
    assert transcript[-1]["text"] == "Can we view it on Friday?"          # still recorded for the agent
