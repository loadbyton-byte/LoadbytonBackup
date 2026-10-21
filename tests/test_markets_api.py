from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.models import Signal

BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"


def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))


def test_intel_endpoints_and_validation():
    c = client()
    assert c.get("/markets/intel").json()["total_leads"] == 0
    c.post("/ingest/signals", json=[{"platform": "web", "external_id": "a1", "source_type": "lead_form", "text": BUY, "phone": "+447911123456"}])
    r = c.get("/markets/intel").json()
    assert r["countries"][0]["id"] == "GB" and r["known_origin_pct"] == 100
    assert c.get("/markets/intel?period=bad").status_code == 422
    assert c.get("/markets/intel?limit=0").status_code == 422
    ok = c.get("/markets/intel/gb")
    assert ok.status_code == 200 and ok.json()["country"]["id"] == "GB" and isinstance(ok.json()["cities"], list)
    assert c.get("/markets/intel/zz").status_code == 404


def test_spend_tagging_over_http():
    c = client()
    base = {"source": "reddit", "period": "2026-10", "amount_aed": 100}
    assert c.post("/spend", json={**base, "city": "London"}).status_code == 200
    assert c.post("/spend", json={**base, "city": "London", "country": "PK"}).status_code == 422
    assert c.post("/spend", json={**base, "country": "XX"}).status_code == 422
    assert c.post("/spend", json=base).status_code == 200
    rep = c.get("/markets/intel").json()
    assert rep["unallocated_spend_aed"] == 100.0 and any(x["id"] == "GB" and x["spend_aed"] == 100.0 for x in rep["countries"])


def test_backfill_over_http():
    c = client()
    assert c.post("/markets/backfill-origin").json() == {"updated": 0, "unresolved": 0}
    app = c.app
    with app.state.db.tx() as cx:
        cx.execute("INSERT INTO leads(first_seen,last_seen,platforms,phone) VALUES('2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','[]','+919876543210')")
    assert c.post("/markets/backfill-origin").json() == {"updated": 1, "unresolved": 0}
    assert c.get("/markets/intel").json()["countries"][0]["id"] == "IN"
