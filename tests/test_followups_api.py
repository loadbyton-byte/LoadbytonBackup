from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.platform.mailer import ConsoleMailer


class Wa:
    def __init__(self):
        self.sent = []

    def send_text(self, to, text):
        self.sent.append((to, text))


def client():
    wa = Wa()
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:"), whatsapp_sender=wa))
    c.wa = wa
    return c


FORM = {"name": "Sara Buyer", "phone": "0501234567", "email": "sara@example.com", "consent": True, "area": "Dubai Marina", "budget": "2M", "looking_to": "buy"}


def touches(c):
    return c.app.state.db.q("SELECT t.*, q.name seq FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id ORDER BY t.id")


def test_consented_capture_enrols_and_queues_first_response():
    c = client()
    assert c.post("/capture", json=FORM).status_code == 200
    assert len(touches(c)) == 7                                                         # 1 first response + 6 nurture steps
    assert c.post("/maintenance/followups").json()["queued"] == 1
    q = c.get("/followups/queue").json()
    assert len(q) == 1 and q[0]["channel"] == "email" and "Sara" in q[0]["body"] and c.wa.sent == []


def test_capture_without_consent_does_not_enrol():
    c = client()
    c.post("/capture", json={**FORM, "consent": False})
    assert touches(c) == []


def test_approve_via_api_sends_and_skip_works():
    c = client()
    c.post("/capture", json=FORM)
    c.post("/maintenance/followups")
    tid = c.get("/followups/queue").json()[0]["id"]
    r = c.post(f"/followups/{tid}/approve", json={"body": "Hello Sara, edited"})
    assert r.status_code == 200 and r.json()["status"] == "dry_run"                     # ConsoleMailer is not a real delivery
    assert c.post(f"/followups/{tid}/approve", json={}).status_code == 409
    assert c.post("/followups/9999/skip").status_code == 409


def test_status_won_viewing_and_reply_stop_the_run():
    c = client()
    c.post("/capture", json=FORM)
    lead = c.get("/leads").json()[0]["id"]
    assert c.post(f"/leads/{lead}/status", json={"status": "contacted"}).status_code == 200
    assert c.app.state.db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='active'")["n"] == 2
    assert c.post(f"/leads/{lead}/status", json={"status": "won"}).status_code == 200
    assert c.app.state.db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='active'")["n"] == 0
    c2 = client()
    c2.post("/capture", json=FORM)
    lead2 = c2.get("/leads").json()[0]["id"]
    c2.post(f"/leads/{lead2}/viewings", json={"start": "2030-01-01T10:00:00+04:00", "duration_min": 30})
    assert c2.app.state.db.one("SELECT stop_reason FROM sequence_runs LIMIT 1")["stop_reason"] == "viewing_booked"


def test_repeat_enquiry_counts_as_reply():
    c = client()
    c.put("/followups/settings", json={"autosend": True})
    c.post("/capture", json=FORM)
    c.post("/maintenance/followups")                                                    # we write to them first...
    c.post("/capture", json=FORM)                                                       # ...and they write again
    assert c.app.state.db.one("SELECT COUNT(*) n FROM sequence_runs WHERE state='stopped' AND stop_reason='replied'")["n"] == 2


def test_pause_run():
    c = client()
    c.post("/capture", json=FORM)
    lead = c.get("/leads").json()[0]["id"]
    assert c.post(f"/followups/runs/{lead}/pause").json() == {"stopped": 2}
    assert c.post("/maintenance/followups").json()["queued"] == 0


def test_sequences_roundtrip_and_validation():
    c = client()
    seqs = c.get("/followups/sequences").json()
    assert [s["name"] for s in seqs] == ["first_response", "nurture_90"]
    seqs[0]["steps"][0]["template_en"] = "Hello {name}!"
    assert c.put("/followups/sequences", json=seqs).status_code == 200
    assert c.get("/followups/sequences").json()[0]["steps"][0]["template_en"] == "Hello {name}!"
    bad = [{"name": "x", "trigger": "new_lead", "enabled": True, "steps": [{"delay_hours": 0, "channel": "sms", "template_en": "hi"}]}]
    assert c.put("/followups/sequences", json=bad).status_code == 422


def test_settings_autosend_toggle():
    c = client()
    assert c.get("/followups/settings").json() == {"autosend": False, "whatsapp_live": True, "email_live": False}      # the injected test sender counts as live
    assert c.put("/followups/settings", json={"autosend": True}).json()["autosend"] is True


def test_whatsapp_inbound_stops_run(monkeypatch):
    c = client()
    c.post("/capture", json=FORM)
    fu = c.app.state.followups
    assert fu.stop_for_phone("+971501234567") == 0                                      # nothing sent yet: the chat that made the lead is not a reply
    c.put("/followups/settings", json={"autosend": True})
    c.post("/maintenance/followups")
    assert fu.stop_for_phone("+971501234567") == 2


def test_stats_endpoint_and_inventory_import_queues_alerts():
    c = client()
    c.post("/capture", json=FORM)
    assert c.get("/followups/stats").json()["queued"] == 0
    csv = "name,developer,area,min_price,max_price,bedrooms,handover\nMarina Gate,Emaar,Dubai Marina,1500000,2500000,\"1,2\",2028\n"
    r = c.post("/inventory", content=csv)
    assert r.json()["imported"] == 1 and r.json()["alerts_queued"] == 1
    assert c.post("/inventory", content=csv).json()["alerts_queued"] == 0                 # re-import updates, it is not a new project
