from datetime import datetime, timedelta, timezone

import pytest

from leadengine.compliance import Compliance
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.followups import Followups, FollowupError
from tests.test_followups_core import NOW, add_lead


class Wa:
    def __init__(self):
        self.sent, self.fail = [], 0

    def send_text(self, to, text):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("boom")
        self.sent.append((to, text))


class Mail:
    def __init__(self):
        self.outbox = []

    def send(self, to, subject, text, html=None):
        self.outbox.append({"to": to, "subject": subject, "text": text})


def make(autosend=True):
    db, s = DB(":memory:"), Settings(db_path=":memory:", api_key="")
    wa, mail = Wa(), Mail()
    fu = Followups(db, Compliance(db, s), s, wa, mail)
    fu.set_autosend(autosend)
    return db, fu, wa, mail


def wa_signal(db, lead, when):
    with db.tx() as c:
        c.execute("INSERT INTO signals(lead_id,platform,external_id,source_type,created_at,ingested_at) VALUES(?,?,?,?,?,?)",
                  (lead, "whatsapp", f"m{lead}{when.timestamp()}", "dm", when.isoformat(), when.isoformat()))


def status(db, step=0, seq="first_response"):
    return db.one("SELECT t.* FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id "
                  "WHERE q.name=? AND t.step_index=?", (seq, step))


def test_autosend_off_queues_for_approval_and_sends_nothing():
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    wa_signal(db, lead, NOW - timedelta(minutes=5))
    fu.enroll(lead, "new_lead", NOW)
    res = fu.tick(NOW)
    assert res["queued"] == 1 and status(db)["status"] == "queued_for_approval" and wa.sent == [] and mail.outbox == []
    assert fu.tick(NOW)["queued"] == 0                                                   # already queued: nothing new


def test_autosend_sends_whatsapp_in_window_with_disclosure_and_opt_out():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    wa_signal(db, lead, NOW - timedelta(minutes=5))
    fu.enroll(lead, "new_lead", NOW)
    assert fu.tick(NOW)["sent"] == 1
    to, text = wa.sent[0]
    assert to == "971501234567" and "Sara" in text and "STOP" in text and "BRN" in text
    assert status(db)["status"] == "sent" and db.one("SELECT COUNT(*) n FROM activities WHERE kind='followup:whatsapp'")["n"] == 1


def test_form_lead_without_whatsapp_window_gets_email_not_whatsapp():
    db, fu, wa, mail = make()
    lead = add_lead(db)                                                                  # no inbound WhatsApp message: business-initiated chat is not allowed
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    assert wa.sent == [] and len(mail.outbox) == 1 and mail.outbox[0]["to"] == "s@example.com"
    assert "STOP" not in mail.outbox[0]["text"] and "unsubscribe" in mail.outbox[0]["text"].lower()


def test_phone_only_lead_outside_window_waits_for_template_approval():
    db, fu, wa, mail = make()
    lead = add_lead(db, email=None)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    t = status(db)
    assert t["status"] == "queued_for_approval" and "24" in t["note"] and wa.sent == []


def test_public_signal_lead_is_skipped_with_reason():
    db, fu, wa, mail = make()
    lead = add_lead(db, consent_status="public_signal")
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    t = status(db)
    assert t["status"] == "skipped" and "opt-in" in t["note"] and wa.sent == [] and mail.outbox == []


def test_opt_out_between_enrol_and_send_sends_nothing():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    with db.tx() as c:
        c.execute("UPDATE leads SET opted_out=1 WHERE id=?", (lead,))
    res = fu.tick(NOW)
    assert res["sent"] == 0 and wa.sent == [] and mail.outbox == [] and status(db)["status"] == "skipped"


def test_due_at_night_is_deferred_to_contact_window():
    db, fu, wa, mail = make()
    lead = add_lead(db, email=None)
    night = datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc)                           # 03:00 UAE
    wa_signal(db, lead, night - timedelta(minutes=1))
    fu.enroll(lead, "new_lead", night)
    res = fu.tick(night)
    t = status(db)
    assert res["deferred"] == 1 and t["status"] == "pending" and t["due_at"] == datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc).isoformat()
    assert wa.sent == []
    assert fu.tick(datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))["sent"] == 1


def test_two_ticks_never_double_send():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    fu.tick(NOW)
    assert len(wa.sent) + len(mail.outbox) == 1


def test_claim_is_atomic():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    tid = status(db)["id"]
    assert fu._claim(tid) is True and fu._claim(tid) is False


def test_weekly_cap_reschedules_extra_touches():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    run = fu.enroll(lead, "new_lead", NOW)[0]
    with db.tx() as c:
        for i in range(3):                                                               # three touches already sent in the last week
            c.execute("INSERT INTO touches(run_id,lead_id,step_index,channel,due_at,status,sent_at) VALUES(?,?,?,?,?,?,?)",
                      (run, lead, 50 + i, "email", NOW.isoformat(), "sent", (NOW - timedelta(days=i + 1)).isoformat()))
    res = fu.tick(NOW)
    t = status(db)
    assert res["deferred"] >= 1 and t["status"] == "pending" and t["due_at"] > NOW.isoformat() and mail.outbox == [] and wa.sent == []


def test_stop_cancels_pending_touches():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.stop(lead, "replied")
    assert fu.tick(NOW)["sent"] == 0
    assert {r["status"] for r in db.q("SELECT status FROM touches")} == {"cancelled"}
    assert {(r["state"], r["stop_reason"]) for r in db.q("SELECT state, stop_reason FROM sequence_runs")} == {("stopped", "replied")}


def test_autosend_switched_off_after_queueing_sends_nothing_without_approval():
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    fu.set_autosend(True)
    fu.set_autosend(False)
    fu.tick(NOW + timedelta(hours=1))
    assert wa.sent == [] and mail.outbox == []


def test_send_errors_retry_then_fail():
    db, fu, wa, mail = make()
    lead = add_lead(db, email=None)
    wa_signal(db, lead, NOW - timedelta(minutes=1))
    wa.fail = 5
    fu.enroll(lead, "new_lead", NOW)
    t = NOW
    for expect in ("pending", "pending", "failed"):
        fu.tick(t)
        row = status(db)
        assert row["status"] == expect
        t += timedelta(hours=2)
    assert row["attempts"] == 3 and "boom" in row["last_error"]
    assert db.one("SELECT COUNT(*) n FROM activities WHERE kind='followup_failed'")["n"] == 1


def test_erased_lead_cancels_touches():
    db, fu, wa, mail = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    with db.tx() as c:
        c.execute("DELETE FROM leads WHERE id=?", (lead,))
    assert fu.tick(NOW)["sent"] == 0
    assert db.one("SELECT COUNT(*) n FROM touches")["n"] == 0                            # cascaded away with the lead


def test_approve_edits_body_regates_and_sends():
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    tid = status(db)["id"]
    assert [q["id"] for q in fu.queue()] == [tid]
    out = fu.approve(tid, "manager", body="Custom hello", now=NOW)
    assert out["status"] == "sent" and mail.outbox[0]["text"].startswith("Custom hello") and status(db)["approved_by"] == "manager"
    with pytest.raises(FollowupError):
        fu.approve(tid, "manager", now=NOW)                                              # not queued any more


def test_approve_refuses_when_lead_opted_out():
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    with db.tx() as c:
        c.execute("UPDATE leads SET opted_out=1 WHERE id=?", (lead,))
    with pytest.raises(FollowupError):
        fu.approve(status(db)["id"], "manager", now=NOW)
    assert mail.outbox == [] and wa.sent == []


def test_skip_marks_skipped():
    db, fu, wa, mail = make(autosend=False)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    fu.skip(status(db)["id"], "manager")
    assert status(db)["status"] == "skipped"


def test_dry_run_senders_are_not_reported_as_sent():
    from leadengine.connectors.whatsapp import DryRunSender
    from leadengine.platform.mailer import ConsoleMailer
    db, s = DB(":memory:"), Settings(db_path=":memory:", api_key="")
    fu = Followups(db, Compliance(db, s), s, DryRunSender(), ConsoleMailer())
    fu.set_autosend(True)
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    assert status(db)["status"] == "dry_run"
