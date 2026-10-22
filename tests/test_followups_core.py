from datetime import datetime, timedelta, timezone

import pytest

from leadengine.compliance import Compliance
from leadengine.config import Settings
from leadengine.connectors.whatsapp import DryRunSender
from leadengine.db import DB
from leadengine.followups import DEFAULT_SEQUENCES, Followups, SequenceError, render
from leadengine.platform.mailer import ConsoleMailer

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)          # 10:00 UAE


def make():
    db, s = DB(":memory:"), Settings(db_path=":memory:", api_key="")
    return db, Followups(db, Compliance(db, s), s, DryRunSender(), ConsoleMailer())


def add_lead(db, **kw):
    v = {"display_name": "Sara", "phone": "+971501234567", "email": "s@example.com", "label": "buyer", "primary_area": "Dubai Marina",
         "budget_max": 2_000_000, "language": "en", "consent_status": "opted_in", "tier": "HOT"}
    v.update(kw)
    with db.tx() as c:
        cur = c.execute(f"INSERT INTO leads({','.join(v)}) VALUES({','.join('?' * len(v))})", list(v.values()))
    return cur.lastrowid


def test_defaults_seeded_once():
    _, fu = make()
    fu.ensure_defaults()
    fu.ensure_defaults()
    names = [s["name"] for s in fu.sequences()]
    assert names == [d["name"] for d in DEFAULT_SEQUENCES] and len(names) == len(set(names))


def test_render_handles_missing_data_and_language_fallback():
    lead = {"display_name": None, "primary_area": None, "budget_max": None, "language": "ar"}
    out = render("Hi {name}, about {area}{budget}. {agent}", None, lead, Settings(db_path=":memory:", api_key=""))
    assert "{" not in out and "}" not in out and out.startswith("Hi there")          # Arabic lead, English-only template: English text, no blanks
    full = render("Hi {name}, {area}{budget}", None, {"display_name": "Sara", "primary_area": "Dubai Marina", "budget_max": 2_000_000, "language": "en"},
                  Settings(db_path=":memory:", api_key=""))
    assert full == "Hi Sara, in Dubai Marina (around AED 2.0M)"       # {area} carries its own "in"
    ar = render("Hi {name}", "مرحبا {name}", {"display_name": "سارة", "language": "ar"}, Settings(db_path=":memory:", api_key=""))
    assert ar == "مرحبا سارة"


def test_enroll_creates_run_and_touches_once():
    db, fu = make()
    lead = add_lead(db)
    runs = fu.enroll(lead, "new_lead", NOW)
    assert len(runs) == 2
    run = runs[0]
    first = db.one("SELECT * FROM touches WHERE run_id=? AND step_index=0", (run,))
    assert first["status"] == "pending" and first["due_at"] == NOW.isoformat()
    n = db.one("SELECT COUNT(*) n FROM touches WHERE run_id=?", (run,))["n"]
    assert n == len(DEFAULT_SEQUENCES[0]["steps"])
    assert fu.enroll(lead, "new_lead", NOW) == []                                       # already enrolled
    assert db.one("SELECT COUNT(*) n FROM sequence_runs")["n"] == 2


def test_nurture_due_dates_follow_delays():
    db, fu = make()
    lead = add_lead(db)
    fu.enroll(lead, "new_lead", NOW)
    seq = next(d for d in DEFAULT_SEQUENCES if d["name"] == "nurture_90")
    rows = db.q("SELECT t.due_at FROM touches t JOIN sequence_runs r ON r.id=t.run_id JOIN sequences q ON q.id=r.sequence_id "
                "WHERE q.name='nurture_90' ORDER BY t.step_index")
    assert [r["due_at"] for r in rows] == [(NOW + timedelta(hours=s["delay_hours"])).isoformat() for s in seq["steps"]]


@pytest.mark.parametrize("bad", [
    [{"name": "x", "trigger": "new_lead", "enabled": True, "steps": [{"delay_hours": 0, "channel": "sms", "template_en": "hi"}]}],
    [{"name": "x", "trigger": "new_lead", "enabled": True, "steps": [{"delay_hours": 99999, "channel": "email", "template_en": "hi"}]}],
    [{"name": "x", "trigger": "new_lead", "enabled": True, "steps": [{"delay_hours": 0, "channel": "email", "template_en": "h" * 1001}]}],
    [{"name": "x", "trigger": "bogus", "enabled": True, "steps": [{"delay_hours": 0, "channel": "email", "template_en": "hi"}]}],
    [{"name": "x", "trigger": "new_lead", "enabled": True, "steps": [{"delay_hours": 0, "channel": "email", "template_en": "hi"}] * 13}],
])
def test_put_sequences_validates(bad):
    _, fu = make()
    with pytest.raises(SequenceError):
        fu.put_sequences(bad)


def test_autosend_default_off_and_toggle():
    _, fu = make()
    assert fu.autosend() is False
    fu.set_autosend(True)
    assert fu.autosend() is True
