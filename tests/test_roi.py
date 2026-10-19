from datetime import datetime, timedelta, timezone

import pytest

from leadengine.models import Signal
from leadengine.roi import record_spend, roi_report

HOT = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"
COLD = "thinking about buying property someday, should i?"


def mk(pipe, n, text=HOT, platform="reddit"):
    return pipe.ingest(Signal(platform=platform, external_id=f"{platform}{n}", text=text, author_handle=f"person_{platform}_{n}")).lead_id


def contact(pipe, lead_id, minutes_after_first_seen):
    first = datetime.fromisoformat(pipe.db.one("SELECT first_seen FROM leads WHERE id=?", (lead_id,))["first_seen"])
    with pipe.db.tx() as c:
        c.execute("INSERT INTO activities(lead_id,ts,kind) VALUES(?,?,?)",
                  (lead_id, (first + timedelta(minutes=minutes_after_first_seen)).isoformat(), "status:contacted"))
    return first


def row(rep, source):
    return next(r for r in rep if r["source"] == source)


def test_roi_arithmetic(pipe):
    ids = [mk(pipe, 1), mk(pipe, 2), mk(pipe, 3, COLD), mk(pipe, 4, COLD)]
    with pipe.db.tx() as c:
        c.execute("UPDATE leads SET status='won' WHERE id=?", (ids[0],))
    record_spend(pipe.db, "reddit", "promo-1", "2026-10", 1000)
    r = row(roi_report(pipe.db), "reddit")
    assert r["leads"] == 4 and r["qualified"] == 2 and r["won"] == 1 and r["spend_aed"] == 1000
    assert (r["cost_per_lead"], r["cost_per_qualified"], r["cost_per_won"], r["win_rate_pct"]) == (250, 500, 1000, 25)


def test_zero_denominators_are_none(pipe):
    record_spend(pipe.db, "facebook", None, "2026-10", 500)          # spend, no leads
    r = row(roi_report(pipe.db), "facebook")
    assert r["leads"] == 0 and r["spend_aed"] == 500
    assert all(r[k] is None for k in ("cost_per_lead", "cost_per_qualified", "cost_per_won", "win_rate_pct",
                                      "within_sla_pct", "median_first_response_min"))
    mk(pipe, 1)                                                      # leads, no spend, none won
    r2 = row(roi_report(pipe.db), "reddit")
    assert r2["cost_per_lead"] is None and r2["cost_per_won"] is None and r2["won"] == 0 and r2["win_rate_pct"] == 0


def test_response_time_and_sla(pipe):
    a, b = mk(pipe, 1), mk(pipe, 2)
    first = contact(pipe, a, 3)                                      # HOT SLA is 5 min
    contact(pipe, b, 20)
    r = row(roi_report(pipe.db, now=first + timedelta(hours=2)), "reddit")
    assert r["median_first_response_min"] == 11.5 and r["within_sla_pct"] == 50


def test_source_is_platform_of_earliest_signal(pipe):
    mk(pipe, 1, platform="reddit")
    pipe.ingest(Signal(platform="instagram", external_id="i1", text=HOT, author_handle="person_reddit_1"))   # soft-link merge
    assert pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 1
    rep = roi_report(pipe.db)
    assert row(rep, "reddit")["leads"] == 1 and not any(r["source"] == "instagram" for r in rep)


def test_negative_spend_rejected(pipe):
    with pytest.raises(ValueError):
        record_spend(pipe.db, "reddit", None, "2026-10", -5)


def test_api_roundtrip():
    from fastapi.testclient import TestClient

    from leadengine.api import create_app
    from leadengine.config import Settings
    from leadengine.db import DB

    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/ingest/signals", json=[{"platform": "reddit", "external_id": "a1", "author_handle": "person_x1", "text": "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"}])
    assert c.post("/spend", json={"source": "reddit", "period": "2026-10", "amount_aed": 800}).json()["id"] > 0
    assert c.post("/spend", json={"source": "reddit", "period": "2026-10", "amount_aed": -1}).status_code == 400
    rows = c.get("/roi").json()
    assert next(r for r in rows if r["source"] == "reddit")["spend_aed"] == 800
