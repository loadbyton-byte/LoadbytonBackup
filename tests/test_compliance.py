from datetime import datetime, timedelta, timezone

import pytest

from leadengine.compliance import GST, Compliance, ComplianceError, assert_connector_allowed
from leadengine.connectors import Connector, ConnectorRegistry
from leadengine.models import Signal

BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer"


def at(hour):  # a UAE-local time on a fixed day
    return datetime(2026, 3, 10, hour, 0, tzinfo=GST).astimezone(timezone.utc)


def test_connector_basis_is_enforced():
    assert_connector_allowed("ok", "official_api")
    with pytest.raises(ComplianceError):
        assert_connector_allowed("evil", "login_scrape")

    class Bad(Connector):
        name, platform, basis = "bad", "facebook", "fake_account_scrape"

        def fetch(self, **p):
            return []

    with pytest.raises(ComplianceError):
        ConnectorRegistry().register(Bad())


def test_public_post_is_not_marketing_consent(pipe):
    pipe.ingest(Signal(platform="reddit", external_id="1", text=BUY, author_handle="someone_x"))
    lead = dict(pipe.db.one("SELECT * FROM leads"))
    lead["platforms"] = ["reddit"]
    c = pipe.compliance
    assert c.can_contact(lead, "public_reply").allowed
    for ch in ("dm", "whatsapp", "sms", "call", "email"):
        d = c.can_contact(lead, ch)
        assert not d.allowed and "opt-in" in d.reason


def test_optin_unlocks_channels_within_hours_only(pipe):
    pipe.ingest(Signal(platform="facebook", external_id="f1", source_type="lead_form", text=BUY, phone="0501234567",
                       author_name="A", consent_marketing=True, consent_text="I agree"))
    lead = dict(pipe.db.one("SELECT * FROM leads"))
    assert lead["consent_status"] == "opted_in"
    c = pipe.compliance
    assert c.can_contact(lead, "whatsapp", at(11)).allowed
    late = c.can_contact(lead, "call", at(22))
    assert not late.allowed and late.retry_after is not None and late.retry_after.astimezone(GST).hour == 9
    assert late.retry_after > at(22)


def test_enquiry_status_allows_reply_but_not_sms_or_dm(pipe):
    pipe.ingest(Signal(platform="portal:bayut", external_id="p1", source_type="portal_inquiry",
                       text="is the 2 bed in Marina available?", phone="0509999999"))
    lead = dict(pipe.db.one("SELECT * FROM leads"))
    assert lead["consent_status"] == "enquiry"
    c = pipe.compliance
    assert c.can_contact(lead, "call", at(11)).allowed and c.can_contact(lead, "email", at(11)).allowed is False  # no email on record
    assert not c.can_contact(lead, "sms", at(11)).allowed


def test_dnc_blocks_calls_even_with_optin(tmp_path, settings, pipe):
    f = tmp_path / "dnc.txt"
    f.write_text("0501234567\n")
    settings.dnc_file = str(f)
    pipe.compliance = Compliance(pipe.db, settings)
    pipe.ingest(Signal(platform="facebook", external_id="f1", source_type="lead_form", text=BUY, phone="0501234567",
                       consent_marketing=True))
    lead = dict(pipe.db.one("SELECT * FROM leads"))
    assert not pipe.compliance.can_contact(lead, "call", at(11)).allowed
    assert pipe.compliance.can_contact(lead, "whatsapp", at(11)).allowed


def test_optout_erases_and_blocks_reingest(pipe):
    sig = dict(platform="facebook", source_type="lead_form", text=BUY, phone="0501234567", email="a@b.com")
    assert pipe.ingest(Signal(external_id="1", **sig)).status == "created"
    assert pipe.opt_out(phone="+971 50 123 4567") == 1
    assert pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 0
    assert pipe.db.one("SELECT COUNT(*) n FROM signals")["n"] == 0
    assert pipe.ingest(Signal(external_id="2", **sig)).status == "suppressed"        # even via a different identifier of theirs
    assert pipe.ingest(Signal(platform="x", external_id="3", text=BUY, email="A@B.com")).status == "suppressed"
    raw = pipe.db.one("SELECT hash FROM suppression").get if False else pipe.db.q("SELECT hash FROM suppression")
    assert all("0501234567" not in r["hash"] and "a@b.com" not in r["hash"] for r in raw)   # hashed, never plaintext


def test_erasure_by_lead_id_and_audit(pipe):
    r = pipe.ingest(Signal(platform="reddit", external_id="1", text=BUY, author_handle="person_abc"))
    assert pipe.erase(r.lead_id)
    assert pipe.ingest(Signal(platform="reddit", external_id="2", text=BUY, author_handle="person_abc")).status == "suppressed"
    assert any(a["action"] == "erased" for a in pipe.db.q("SELECT action FROM audit_log"))


def test_retention_purges_public_but_keeps_recent_optin(pipe):
    pipe.ingest(Signal(platform="reddit", external_id="old", text=BUY, author_handle="old_person", created_at=datetime.now(timezone.utc)))
    pipe.ingest(Signal(platform="facebook", external_id="new", source_type="lead_form", text=BUY, phone="0507777777", consent_marketing=True))
    future = datetime.now(timezone.utc) + timedelta(days=pipe.s.retention_days_public + 5)
    res = pipe.compliance.purge_expired(now=future)
    assert res["public_deleted"] == 1 and res["optin_deleted"] == 0
    assert pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 1
