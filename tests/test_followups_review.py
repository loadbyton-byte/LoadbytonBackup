"""Regression tests for the independent review of the follow-up engine."""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.followups import DEFAULT_SEQUENCES, FollowupError, SequenceError, render
from leadengine.inventory import import_projects_csv
from tests.test_followups_core import NOW, add_lead
from tests.test_followups_tick import make, status, wa_signal

CSV = "name,developer,area,min_price,max_price,bedrooms,handover\nMarina Gate,Emaar,Dubai Marina,1500000,2500000,\"1,2\",2028\n"


def seq_status(db, seq, step=0):
    return db.one("SELECT t.* FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id WHERE q.name=? AND t.step_index=?", (seq, step))


def test_enquiry_leads_only_get_the_first_response_never_nurture():            # C1
    db, fu, wa, mail = make()
    lead = add_lead(db, consent_status="enquiry")
    runs = fu.enroll(lead, "new_lead", NOW)
    names = {r["name"] for r in db.q("SELECT q.name FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id")}
    assert len(runs) == 1 and names == {"first_response"}


def test_a_nurture_touch_is_skipped_if_consent_is_only_enquiry_at_send_time():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    with db.tx() as c:
        c.execute("UPDATE leads SET consent_status='enquiry' WHERE id=?", (lead,))
    fu.tick(NOW + timedelta(days=3))
    assert seq_status(db, "nurture_90")["status"] == "skipped" and "opt-in" in seq_status(db, "nurture_90")["note"]


def test_dnc_blocks_whatsapp_too():                                             # C2
    db, fu, wa, mail = make()
    lead = add_lead(db, email=None)
    wa_signal(db, lead, NOW - timedelta(minutes=1))
    fu.comp.dnc = lambda: {"+971501234567"}
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    assert wa.sent == [] and status(db)["status"] == "skipped" and "Do-Not-Call" in status(db)["note"]


def test_every_email_carries_a_working_unsubscribe_link_and_header():          # C3
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    m = mail.outbox[0]
    url = fu.unsubscribe_url(lead)
    assert url in m["text"] and m["headers"]["List-Unsubscribe"] == f"<{url}>" and m["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert fu.verify_unsubscribe(url.split("t=")[1]) == lead
    assert fu.verify_unsubscribe(f"{lead}.{'0' * 32}") is None and fu.verify_unsubscribe("junk") is None and fu.verify_unsubscribe(f"{lead + 1}." + url.split(".")[-1]) is None


def test_unsubscribe_endpoint_erases_and_suppresses():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/capture", json={"name": "Sara", "phone": "0501234567", "email": "sara@example.com", "consent": True})
    lead = c.get("/leads").json()[0]["id"]
    tok = c.app.state.followups.unsubscribe_url(lead).split("t=")[1]
    assert "Unsubscribe" in c.get(f"/unsubscribe?t={tok}").text and c.get("/leads").json() != []        # GET alone changes nothing
    assert c.post(f"/unsubscribe?t={tok}").status_code == 200
    assert c.get("/leads").json() == [] and c.get("/unsubscribe?t=bad").status_code == 404 and c.post("/unsubscribe?t=bad").status_code == 404


def test_bookkeeping_failure_after_a_send_never_causes_a_second_send():        # C4
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    db.audit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked"))
    fu.tick(NOW)
    fu.tick(NOW + timedelta(hours=2))
    assert len(mail.outbox) == 1 and status(db)["status"] == "sent"


def test_approving_late_rechecks_the_whatsapp_window():                          # I1b
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db, email=None)
    wa_signal(db, lead, NOW - timedelta(minutes=1))
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    assert status(db)["status"] == "queued_for_approval"
    with pytest.raises(FollowupError):
        fu.approve(status(db)["id"], "manager", now=NOW + timedelta(hours=30))
    assert wa.sent == [] and status(db)["status"] == "queued_for_approval"


def test_approvals_respect_the_weekly_cap_and_queueing_counts_too():            # I2
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    t = NOW + timedelta(days=100)                                                # every touch is due
    res = fu.tick(t)
    assert res["queued"] == 3 and res["deferred"] >= 1                          # queued touches count against the cap
    for q in fu.queue():
        fu.approve(q["id"], "manager", now=t)
    extra = db.one("SELECT t.id FROM touches t WHERE t.status='pending' LIMIT 1")
    db.conn.execute("UPDATE touches SET status='queued_for_approval' WHERE id=?", (extra["id"],))
    db.conn.commit()
    with pytest.raises(FollowupError):
        fu.approve(extra["id"], "manager", now=t)
    assert len(mail.outbox) == 3


def test_stop_during_processing_wins():                                          # I3
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    tid = status(db)["id"]
    fu.stop(lead, "replied")
    assert fu.queue() == [] and status(db)["status"] == "cancelled"
    with pytest.raises(FollowupError):
        fu.approve(tid, "manager", now=NOW)
    t2 = status(db, 0, "nurture_90")
    db.conn.execute("UPDATE touches SET status='processing' WHERE id=?", (t2["id"],))
    db.conn.commit()
    fu.stop(lead, "viewing_booked")
    assert db.one("SELECT status FROM touches WHERE id=?", (t2["id"],))["status"] == "cancelled"
    assert fu._set(t2["id"], status="pending") is False                          # a late write cannot resurrect it


def test_paused_or_closed_leads_get_no_offers_and_no_new_sequences():            # I4
    db, fu, wa, mail = make()
    paused, lost = add_lead(db, primary_area="Dubai Marina"), add_lead(db, primary_area="Dubai Marina", phone="+971502222222", email="b@example.com", status="lost")
    fu.stop(paused, "paused")
    import_projects_csv(db, CSV)
    assert fu.alert_new_project(db.one("SELECT id FROM projects")["id"], NOW, golden_min=2_000_000) == []
    assert fu.enroll(paused, "new_lead", NOW) == [] and fu.enroll(lost, "new_lead", NOW) == []


def test_disabled_sequences_stop_sending():                                      # I5
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    seqs = fu.sequences()
    for s in seqs:
        s["enabled"] = False
    fu.put_sequences(seqs)
    assert fu.tick(NOW + timedelta(days=100))["sent"] == 0 and mail.outbox == []


def test_shortening_a_sequence_cancels_the_removed_steps():                      # I6
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    seqs = fu.sequences()
    seqs[1]["steps"] = seqs[1]["steps"][:2]
    fu.put_sequences(seqs)
    gone = db.q("SELECT status FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id WHERE q.name='nurture_90' AND t.step_index>=2")
    assert gone and all(r["status"] == "skipped" for r in gone)
    assert fu.tick(NOW + timedelta(days=300))["deferred"] == 0


def test_the_qualification_chat_is_not_a_reply():                                # I7
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    assert fu.stop_if_replied(lead) == 0 and fu.tick(NOW)["sent"] == 1
    assert fu.stop_if_replied(lead) == 2


def test_a_worker_that_died_mid_send_is_flagged_not_resent():                    # I8
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    tid = status(db)["id"]
    assert fu._claim(tid, NOW)
    fu.tick(NOW + timedelta(hours=2))
    row = db.one("SELECT status, note FROM touches WHERE id=?", (tid,))
    assert row["status"] == "failed" and "check before resending" in row["note"] and mail.outbox == []


def test_first_response_metric_ignores_nurture_and_alerts():                     # I9
    db, fu, wa, mail = make()
    lead = add_lead(db, first_seen=(NOW - timedelta(days=5)).isoformat(), tier="HOT")
    fu.enroll(lead, "new_lead", NOW, exclude=("first_response",))              # a WhatsApp-bot lead: only nurture exists
    fu.tick(NOW + timedelta(days=3))
    assert fu.stats()["first_response_median_minutes"] is None


def test_email_waits_for_contact_hours():                                        # M1
    db, fu, wa, mail = make()
    lead = add_lead(db)
    night = datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc)                 # 03:00 UAE
    fu.enroll(lead, "new_lead", night)
    assert fu.tick(night)["deferred"] >= 1 and mail.outbox == []
    assert fu.tick(datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))["sent"] == 1


def test_an_edited_approval_keeps_the_disclosure_and_unsubscribe():              # M2
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    fu.approve(status(db)["id"], "manager", body="Just this.", now=NOW)
    sent = mail.outbox[0]["text"]
    assert sent.startswith("Just this.") and "BRN" in sent and fu.unsubscribe_url(lead) in sent


@pytest.mark.parametrize("bad", [
    [{"name": "x", "trigger": "new_lead", "steps": ["nope"]}],
    [{"name": "x", "trigger": "new_lead", "enabled": "false", "steps": [{"delay_hours": 0, "channel": "email", "template_en": "hi"}]}],
    [{"name": "x", "trigger": "new_lead", "min_consent": "everyone", "steps": [{"delay_hours": 0, "channel": "email", "template_en": "hi"}]}],
    [{"name": "x", "trigger": "new_lead", "steps": [{"delay_hours": None, "channel": "email", "template_en": "hi"}]}],
])
def test_editor_validation_holes_are_422_not_500(bad):                           # M3
    _, fu, _, _ = make()
    with pytest.raises(SequenceError):
        fu.put_sequences(bad)


def test_recording_consent_later_starts_nurture():                               # M4
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/ingest/signals", json=[{"platform": "web", "external_id": "e1", "source_type": "lead_form", "text": "2BR Dubai Marina", "author_name": "Sara",
                                     "phone": "+971501234567"}])
    lead = c.get("/leads").json()[0]["id"]
    names = lambda: {r["name"] for r in c.app.state.db.q("SELECT q.name FROM sequence_runs r JOIN sequences q ON q.id=r.sequence_id")}      # noqa: E731
    assert names() == {"first_response"}
    c.post(f"/leads/{lead}/consent", json={"evidence_text": "ticked the box"})
    assert names() == {"first_response", "nurture_90"}


def test_arabic_greeting_without_a_name_has_no_stray_comma():                    # M7
    out = render("Hi", "مرحباً {name}، شكراً", {"display_name": None, "language": "ar"}, Settings(db_path=":memory:", api_key=""))
    assert out == "مرحباً، شكراً"
