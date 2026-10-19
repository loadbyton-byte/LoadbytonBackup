import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from leadengine.connectors.apify import ApifyConnector
from leadengine.connectors.forms import (FormWebhookConnector, MetaLeadAdsConnector, parse_csv, parse_portal_email,
                                         verify_hmac)
from leadengine.connectors.reddit import RedditConnector
from leadengine.demo import demo_signals
from leadengine.models import Signal
from leadengine.radar import demand_radar

BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M"


def test_dedupe_and_noise_not_stored(pipe):
    s = Signal(platform="reddit", external_id="1", text=BUY, author_handle="user_one")
    assert pipe.ingest(s).status == "created" and pipe.ingest(s).status == "duplicate"
    assert pipe.ingest(Signal(platform="reddit", external_id="2", text="nice sunset in Dubai Marina today")).status == "discarded"
    assert pipe.db.one("SELECT COUNT(*) n FROM signals")["n"] == 1
    assert pipe.db.one("SELECT value FROM stats WHERE key='discarded_noise'")["value"] == 1


def test_phone_links_identities_and_merges_leads(pipe):
    a = pipe.ingest(Signal(platform="reddit", external_id="1", text=BUY, author_handle="user_one"))
    b = pipe.ingest(Signal(platform="facebook", external_id="2", source_type="lead_form", text="villa Dubai Hills buy 3M",
                           phone="0501234567", author_handle="fb_person"))
    assert a.lead_id != b.lead_id
    # a new signal carrying BOTH identifiers proves they are one person -> records merge
    c = pipe.ingest(Signal(platform="reddit", external_id="3", text="still want to buy in Dubai Marina asap", author_handle="user_one", phone="050 123 4567"))
    assert c.status == "merged" and pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 1
    assert pipe.db.one("SELECT COUNT(*) n FROM signals")["n"] == 3


def test_cross_platform_handle_link_requires_shared_area(pipe):
    pipe.ingest(Signal(platform="reddit", external_id="1", text=BUY, author_handle="dubai_buyer77"))
    pipe.ingest(Signal(platform="instagram", external_id="2", text="want to buy an apartment in Downtown Dubai", author_handle="dubai_buyer77"))
    assert pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 2                 # different area => not merged
    r = pipe.ingest(Signal(platform="tiktok", external_id="3", text="planning to buy in Dubai Marina", author_handle="dubai_buyer77"))
    assert r.status == "merged"
    lead = pipe.db.one("SELECT breakdown FROM leads WHERE id=?", (r.lead_id,))
    assert json.loads(lead["breakdown"])["cross_platform_bonus"] == 5


def test_lead_form_without_text_still_becomes_lead_with_entities(pipe):
    r = pipe.ingest(Signal(platform="facebook", external_id="f", source_type="lead_form", author_name="Demo", phone="0501112222",
                           extra={"budget": "AED 2.2M", "preferred_area": "Dubai Hills", "bedrooms": "3", "looking_to": "buy"}))
    lead = pipe.db.one("SELECT * FROM leads WHERE id=?", (r.lead_id,))
    assert lead["primary_area"] == "Dubai Hills Estate" and lead["budget_max"] == 2_200_000 and lead["bedrooms"] == 3
    assert lead["label"] == "buyer"


def test_demo_dataset_end_to_end(pipe):
    res = pipe.ingest_many(demo_signals())
    assert res["created"] >= 12 and res["merged"] >= 1 and res["discarded"] >= 2
    top = pipe.db.one("SELECT * FROM leads ORDER BY score DESC LIMIT 1")
    assert top["tier"] == "HOT" and top["primary_area"] == "Dubai Marina" and "instagram" in top["platforms"]
    assert pipe.db.one("SELECT COUNT(*) n FROM leads WHERE phone IS NOT NULL")["n"] == 3


def test_rescore_decays_old_leads(pipe):
    pipe.ingest(Signal(platform="reddit", external_id="1", text=BUY, author_handle="user_one",
                       created_at=datetime.now(timezone.utc) - timedelta(days=1)))
    before = pipe.db.one("SELECT score FROM leads")["score"]
    pipe.recompute(1, now=datetime.now(timezone.utc) + timedelta(days=60))
    assert pipe.db.one("SELECT score FROM leads")["score"] < before


def test_radar_flags_heating_areas(pipe):
    now = datetime.now(timezone.utc)
    for i in range(5):
        pipe.ingest(Signal(platform="reddit", external_id=f"c{i}", text=f"Looking to buy in Dubai South budget 1M u{i}", author_handle=f"cur_user_{i}", created_at=now - timedelta(days=2)))
    pipe.ingest(Signal(platform="reddit", external_id="p0", text="Looking to buy in Dubai South budget 1M", author_handle="prev_user_0", created_at=now - timedelta(days=20)))
    row = next(r for r in demand_radar(pipe.db, days=14) if r["area"] == "Dubai South")
    assert row["signals"] == 5 and row["previous_period"] == 1 and row["trend"] == "heating" and row["median_budget_aed"] == 1_000_000


# ------------------------------------------------------------------ connectors
def test_reddit_connector_official_api_flow(settings):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        if "access_token" in url:
            return {"access_token": "T"}
        assert kw["headers"]["Authorization"] == "bearer T"
        return {"data": {"children": [{"data": {"id": "abc", "title": "Looking to buy in JVC", "selftext": "budget 900k",
                                                 "author": "u1", "permalink": "/r/dubai/abc", "created_utc": 1_700_000_000, "subreddit": "dubai"}}]}}

    sigs = list(RedditConnector(settings, http=fake).fetch(subreddits=["dubai"], queries=["looking to buy"]))
    assert len(sigs) == 1 and sigs[0].external_id == "abc" and sigs[0].platform == "reddit" and "oauth.reddit.com" in calls[1]


def test_meta_webhook_signature_and_lead_fetch(settings):
    body = b'{"entry":[]}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_hmac("s3cret", body, sig) and not verify_hmac("s3cret", body, "sha256=bad") and not verify_hmac("", body, sig)

    def fake(url, **kw):
        assert "L1" in url and "access_token=tok" in url
        return {"id": "L1", "created_time": "2026-03-10T10:00:00+0000", "platform": "ig", "field_data": [
            {"name": "full_name", "values": ["Aisha K"]}, {"name": "phone_number", "values": ["+971501234567"]},
            {"name": "budget", "values": ["AED 1.5M"]}, {"name": "preferred_area", "values": ["Dubai Marina"]},
            {"name": "consent", "values": ["yes"]}]}

    payload = {"entry": [{"changes": [{"field": "leadgen", "value": {"leadgen_id": "L1", "ad_id": "AD9"}}]}]}
    [s] = MetaLeadAdsConnector(settings, http=fake).parse_webhook(payload)
    assert s.platform == "instagram" and s.author_name == "Aisha K" and s.consent_marketing and s.campaign == "AD9"
    assert s.extra == {"budget": "AED 1.5M", "preferred_area": "Dubai Marina"}


def test_form_webhook_and_portal_email_and_csv(settings, pipe):
    s = FormWebhookConnector(settings).parse("tiktok", {"id": "T1", "fields": {"Full Name": "Omar", "Phone": "0501234567",
                                                                           "Budget": "900k", "Area": "Dubai South"}, "campaign": "c1"})
    assert s.platform == "tiktok" and s.author_name == "Omar" and not s.consent_marketing
    body = "Name: Sara\nPhone: 0507654321\nEmail: s@x.com\nMessage: Is it still available? Can I view?\nReference: BAY-123"
    p = parse_portal_email("bayut", "New enquiry: 2BR Dubai Marina", body, "msg1")
    assert p.phone == "0507654321" and p.campaign == "BAY-123" and p.source_type == "portal_inquiry"
    rows = parse_csv("name,phone,message,consent\nLayla,0501112233,looking to buy villa in Dubai Hills 3M,yes\n")
    assert rows[0].consent_marketing and rows[0].source_type == "import"
    assert pipe.ingest(rows[0]).status == "created"


def test_apify_mapping(settings):
    def fake(url, **kw):
        assert "run-sync-get-dataset-items" in url
        return [{"id": "1", "text": "Looking to buy a villa in Dubai Hills", "author": {"username": "ln_user"}, "url": "https://x/1",
                 "createdAt": "2026-03-01T10:00:00Z"}, {"id": "2"}]

    sigs = list(ApifyConnector(settings, http=fake).fetch(actor_id="a/b", platform="linkedin"))
    assert len(sigs) == 1 and sigs[0].author_handle == "ln_user" and sigs[0].platform == "linkedin"
