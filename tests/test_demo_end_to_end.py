from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB


def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))


def test_demo_seed_exercises_every_feature():
    c = client()
    res = c.post("/demo/seed").json()
    assert res["created"] >= 12 and res["agents"] >= 3 and res["projects"] >= 3
    agents = c.get("/agents").json()
    assert len(agents) >= 3 and sum(a["open_leads"] for a in agents) >= 2
    hot = c.get("/leads?tier=HOT").json()
    assert hot and all(l["assigned_agent_id"] for l in hot)
    assert any(r["spend_aed"] > 0 and r["leads"] > 0 for r in c.get("/roi").json())
    assert any(r["median_first_response_min"] is not None for r in c.get("/roi").json())
    assert c.get("/viewings").json() and "BEGIN:VEVENT" in c.get("/viewings.ics").text
    assert c.get("/tasks/today").json()
    assert c.get("/inventory").json()
    top = hot[0]["id"]
    assert c.get(f"/leads/{top}/matches").json() and c.get(f"/leads/{top}/deal-sheet").json()["cash_needed_total"] > 0


def test_demo_seed_is_idempotent():
    c = client()
    c.post("/demo/seed")
    before = (len(c.get("/agents").json()), len(c.get("/inventory").json()), len(c.get("/viewings").json()), len(c.get("/leads").json()))
    again = c.post("/demo/seed").json()
    assert again["created"] == 0 and again["agents"] == 0 and again["projects"] == 0
    assert (len(c.get("/agents").json()), len(c.get("/inventory").json()), len(c.get("/viewings").json()), len(c.get("/leads").json())) == before
