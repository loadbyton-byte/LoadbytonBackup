from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.roi import roi_report

FORM = {"name": "Sara Buyer", "phone": "0501234567", "email": "sara@example.com", "consent": True}


def setup():
    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/capture", json=FORM)
    c.lead = c.get("/leads").json()[0]["id"]
    c.post("/spend", json={"source": "web", "period": "2026-10", "amount_aed": 1000})
    return c


def row(c):
    return next(r for r in roi_report(c.app.state.db) if r["source"] == "web")


def test_revenue_and_commission_come_from_paid_deals_only():
    c = setup()
    did = c.post("/deals", json={"lead_id": c.lead, "agent_id": 1, "kind": "sale", "value_aed": 2_000_000}).json()["id"]
    assert row(c)["revenue_aed"] == 0 and row(c)["commission_aed"] == 0 and row(c)["commission_per_spend"] == 0
    c.post(f"/deals/{did}/stage", json={"stage": "paid"})
    r = row(c)
    assert r["revenue_aed"] == 2_000_000 and r["commission_aed"] == 40_000 and r["commission_per_spend"] == 40.0
    c.post(f"/deals/{did}/stage", json={"stage": "offer_made"})                              # moved back: no longer revenue
    assert row(c)["revenue_aed"] == 0


def test_no_spend_means_null_ratio_not_an_error():
    c = setup()
    c.app.state.db.conn.execute("DELETE FROM spend")
    c.app.state.db.conn.commit()
    did = c.post("/deals", json={"lead_id": c.lead, "agent_id": 1, "kind": "sale", "value_aed": 1_000_000}).json()["id"]
    c.post(f"/deals/{did}/stage", json={"stage": "paid"})
    r = row(c)
    assert r["commission_per_spend"] is None and r["revenue_aed"] == 1_000_000


def test_overview_has_pipeline_numbers():
    c = setup()
    c.post("/deals", json={"lead_id": c.lead, "agent_id": 1, "kind": "sale", "value_aed": 2_000_000})
    o = c.get("/overview").json()
    assert o["pipeline_value_aed"] == 2_000_000 and o["expected_commission_aed"] == 40_000
