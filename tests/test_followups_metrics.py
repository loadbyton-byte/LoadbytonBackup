from datetime import datetime, timedelta, timezone

from leadengine.inventory import import_projects_csv
from tests.test_followups_core import NOW, add_lead
from tests.test_followups_tick import make, status, wa_signal

CSV = "name,developer,area,min_price,max_price,bedrooms,handover\nMarina Gate,Emaar,Dubai Marina,1500000,2500000,\"1,2\",2028\n"


def test_stats_are_null_safe_when_nothing_happened():
    _, fu, _, _ = make()
    s = fu.stats()
    assert s["first_response_median_minutes"] is None and s["within_sla_pct"] is None and s["queued"] == 0 and s["per_step"] == []


def test_first_response_time_sla_and_reply_attribution():
    db, fu, wa, mail = make()
    lead = add_lead(db, first_seen=(NOW - timedelta(minutes=3)).isoformat(), tier="HOT")
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)                                                                          # first response goes out 3 minutes after first_seen
    fu.stop(lead, "replied")
    s = fu.stats()
    assert s["first_response_median_minutes"] == 3.0 and s["within_sla_pct"] == 100
    first = next(r for r in s["per_step"] if r["sequence"] == "first_response" and r["step"] == 0)
    assert first["sent"] == 1 and first["replied"] == 1


def test_slow_response_is_outside_sla():
    db, fu, wa, mail = make()
    lead = add_lead(db, first_seen=(NOW - timedelta(minutes=30)).isoformat(), tier="HOT")          # HOT SLA is 5 minutes
    fu.enroll(lead, "new_lead", NOW)
    fu.tick(NOW)
    assert fu.stats()["within_sla_pct"] == 0


def test_new_project_alerts_matching_consented_leads_once_a_week():
    db, fu, wa, mail = make()
    match = add_lead(db, primary_area="Dubai Marina", budget_max=2_000_000)
    wrong_area = add_lead(db, primary_area="Business Bay", phone="+971502222222", email="b@example.com")
    no_consent = add_lead(db, primary_area="Dubai Marina", consent_status="enquiry", phone="+971503333333", email="c@example.com")
    import_projects_csv(db, CSV)
    pid = db.one("SELECT id FROM projects")["id"]
    assert fu.alert_new_project(pid, NOW, golden_min=2_000_000) == [match]
    assert fu.alert_new_project(pid, NOW, golden_min=2_000_000) == []                     # same project: already alerted
    import_projects_csv(db, CSV.replace("Marina Gate", "Marina Two"))
    pid2 = db.one("SELECT id FROM projects WHERE name='Marina Two'")["id"]
    assert fu.alert_new_project(pid2, NOW + timedelta(days=2), golden_min=2_000_000) == []          # weekly cap: one alert per lead per week
    assert fu.alert_new_project(pid2, NOW + timedelta(days=8), golden_min=2_000_000) == [match]
    assert wrong_area not in [r["lead_id"] for r in db.q("SELECT lead_id FROM sequence_runs")] and no_consent not in [r["lead_id"] for r in db.q("SELECT lead_id FROM sequence_runs")]


def test_alert_text_mentions_project_and_goes_through_the_gate():
    db, fu, wa, mail = make(autosend=False)
    add_lead(db, primary_area="Dubai Marina", budget_max=2_000_000)
    import_projects_csv(db, CSV.replace("Marina Gate", "Marina {name} Gate"))
    fu.alert_new_project(db.one("SELECT id FROM projects")["id"], NOW, golden_min=2_000_000)
    fu.tick(NOW)
    body = fu.queue()[0]["body"]
    assert "Marina" in body and "{" not in body and ("unsubscribe" in body.lower() or "STOP" in body)
    assert "Business Bay" not in body


def test_alert_sequences_are_not_listed_in_the_editor():
    db, fu, wa, mail = make()
    add_lead(db, primary_area="Dubai Marina", budget_max=2_000_000)
    import_projects_csv(db, CSV)
    fu.alert_new_project(db.one("SELECT id FROM projects")["id"], NOW, golden_min=2_000_000)
    assert [s["name"] for s in fu.sequences()] == ["first_response", "nurture_90"]
