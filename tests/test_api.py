import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB


@pytest.fixture()
def client():
    s = Settings(db_path=":memory:", api_key="k", meta_app_secret="s3cret", meta_verify_token="vt", form_webhook_secret="fsecret",
                 agent_name="Sara", agency_name="Acme RE", brn="12345", trakheesi="TRK-1")
    return TestClient(create_app(s, DB(":memory:")))


H = {"x-api-key": "k"}


def test_auth_required_but_health_and_optout_open(client):
    assert client.get("/leads").status_code == 401
    assert client.get("/leads", headers=H).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.post("/optout", json={"email": "x@y.com"}).status_code == 200


def test_seed_list_detail_filters(client):
    assert client.post("/demo/seed", headers=H).json()["created"] >= 12
    hot = client.get("/leads?tier=HOT", headers=H).json()
    assert hot and all(l["tier"] == "HOT" for l in hot)
    assert client.get("/leads?area=Dubai%20Marina&min_score=60", headers=H).json()
    detail = client.get(f"/leads/{hot[0]['id']}", headers=H).json()
    assert detail["signals"] and "intent" in detail["breakdown"]
    assert client.get("/leads/9999", headers=H).status_code == 404
    assert client.get("/radar", headers=H).json()[0]["signals"] >= 1
    assert client.get("/stats", headers=H).json()["tiers"]["HOT"] >= 1


def test_outreach_draft_is_gated_and_bilingual(client):
    client.post("/demo/seed", headers=H)
    leads = client.get("/leads", headers=H).json()
    public = next(l for l in leads if l["consent_status"] == "public_signal")
    d = client.post(f"/leads/{public['id']}/outreach?channel=whatsapp", headers=H).json()
    assert not d["compliance"]["allowed"] and d["requires_human_approval"]
    d = client.post(f"/leads/{public['id']}/outreach?channel=public_reply", headers=H).json()
    assert d["compliance"]["allowed"] and "TRK-1" in d["body"] and "BRN 12345" in d["body"]
    ar = next(l for l in leads if l["language"] == "ar")
    assert "مرحباً" in client.post(f"/leads/{ar['id']}/outreach?channel=public_reply", headers=H).json()["body"]
    optin = next(l for l in leads if l["consent_status"] == "opted_in")
    wa = client.post(f"/leads/{optin['id']}/outreach?channel=whatsapp", headers=H).json()
    assert "STOP" in wa["body"] and "Sara" in wa["body"]


def test_meta_webhook_verification_and_signature(client):
    ok = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.text == "42"
    assert client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403
    assert client.post("/webhooks/meta", content=b"{}", headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401


def test_signed_form_webhook_creates_lead_with_enquiry_status(client):
    body = json.dumps({"id": "T1", "fields": {"Full Name": "Omar", "Phone": "0501234567", "Budget": "AED 900k",
                                                "Area": "Dubai South", "looking_to": "buy off plan"}}).encode()
    sig = hmac.new(b"fsecret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/form/tiktok", content=body, headers={"x-signature": "nope"}).status_code == 401
    r = client.post("/webhooks/form/tiktok", content=body, headers={"x-signature": sig}).json()
    assert r["status"] == "created"
    lead = client.get(f"/leads/{r['lead_id']}", headers=H).json()
    assert lead["consent_status"] == "enquiry" and lead["primary_area"] == "Dubai South" and lead["phone"] == "+971501234567"


def test_consent_status_and_erasure_flow(client):
    client.post("/demo/seed", headers=H)
    lead = next(l for l in client.get("/leads", headers=H).json() if l["consent_status"] == "public_signal")
    assert client.post(f"/leads/{lead['id']}/consent", json={"evidence_text": "Replied YES to opt-in DM on 2026-03-01"}, headers=H).status_code == 200
    assert client.get(f"/leads/{lead['id']}", headers=H).json()["consent_evidence"]["text"].startswith("Replied YES")
    assert client.post(f"/leads/{lead['id']}/status", json={"status": "contacted", "note": "called"}, headers=H).status_code == 200
    assert client.post(f"/leads/{lead['id']}/status", json={"status": "bogus"}, headers=H).status_code == 400
    assert client.delete(f"/leads/{lead['id']}", headers=H).json() == {"erased": True}
    assert client.get(f"/leads/{lead['id']}", headers=H).status_code == 404
    assert [a["action"] for a in client.get("/audit", headers=H).json()][:1] == ["erased"]


def test_portal_email_and_csv_ingest(client):
    r = client.post("/ingest/portal-email", headers=H, json={"portal": "bayut", "subject": "Enquiry 2BR Marina",
                    "body": "Name: Sara\nPhone: 0507654321\nMessage: still available?\nReference: BAY-1", "message_id": "m1"}).json()
    assert r["status"] == "created"
    csv = "name,phone,message,consent\nLayla,0501112233,looking to buy villa in Dubai Hills 3M,yes\n"
    assert client.post("/ingest/csv", headers=H, content=csv).json()["created"] == 1


def test_connector_listing_and_missing_credentials(client):
    names = {c["name"] for c in client.get("/connectors", headers=H).json()}
    assert {"reddit", "apify", "meta_lead_ads", "form_webhook"} <= names
    assert client.post("/connectors/reddit/run", headers=H, json={}).status_code == 400    # no REDDIT_* configured
    assert client.post("/connectors/nope/run", headers=H, json={}).status_code == 404
