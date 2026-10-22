from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB

FORM = {"name": "Sara Buyer", "phone": "0501234567", "email": "sara@example.com", "consent": True, "area": "Dubai Marina", "looking_to": "buy"}


def client():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/capture", json=FORM)
    c.lead = c.get("/leads").json()[0]["id"]
    return c


def test_create_get_patch_move_over_http():
    c = client()
    r = c.post("/deals", json={"lead_id": c.lead, "agent_id": 1, "kind": "sale", "value_aed": 2_000_000, "title": "Marina 2BR"})
    assert r.status_code == 200 and r.json()["commission_aed"] == 40_000
    did = r.json()["id"]
    assert c.get(f"/deals/{did}").json()["events"][0]["to_stage"] == "viewing_done"
    assert c.patch(f"/deals/{did}", json={"value_aed": 1_000_000}).json()["commission_aed"] == 20_000
    assert c.post(f"/deals/{did}/stage", json={"stage": "lost"}).status_code == 422                 # reason needed
    assert c.post(f"/deals/{did}/stage", json={"stage": "offer_accepted"}).json()["stage"] == "offer_accepted"
    assert c.get("/leads").json()[0]["status"] == "won"
    assert [d["id"] for d in c.get("/deals?stage=offer_accepted").json()] == [did] and c.get("/deals?stage=lost").json() == []


def test_errors_are_4xx_not_500():
    c = client()
    assert c.post("/deals", json={"lead_id": c.lead, "kind": "sale", "value_aed": -1}).status_code == 422
    assert c.post("/deals", json={"lead_id": 999, "kind": "sale", "value_aed": 1000000}).status_code == 404
    assert c.get("/deals/999").status_code == 404 and c.patch("/deals/999", json={}).status_code == 404
    assert c.post("/deals/999/stage", json={"stage": "paid"}).status_code == 404
    assert c.patch("/deals/1", json={"stage": "paid"}).status_code in (404, 422)
    assert c.post("/deals", content="[1]", headers={"content-type": "application/json"}).status_code == 422
    assert c.get("/deals/leaderboard?period=oops").status_code == 422


def test_summary_leaderboard_and_settings():
    c = client()
    d = c.post("/deals", json={"lead_id": c.lead, "agent_id": 3, "kind": "sale", "value_aed": 2_000_000}).json()
    assert c.get("/deals/summary").json()["pipeline_value_aed"] == 2_000_000
    c.post(f"/deals/{d['id']}/stage", json={"stage": "paid"})
    assert c.get("/deals/leaderboard").json()[0]["commission_aed"] == 40_000
    assert c.put("/deals/settings", json={"commission_sale_pct": 3}).json()["commission_sale_pct"] == 3
    assert c.put("/deals/settings", json={"vat_pct": 500}).status_code == 422 and c.get("/deals/settings").json()["vat_pct"] == 5


def test_agent_defaults_to_the_leads_assigned_agent():
    c = client()
    aid = c.post("/agents", json={"name": "Layla", "languages": ["en"], "areas": ["Dubai Marina"]}).json()["id"]
    c.post(f"/leads/{c.lead}/assign")
    d = c.post("/deals", json={"lead_id": c.lead, "kind": "sale", "value_aed": 1_000_000}).json()
    assert d["agent_id"] == aid
