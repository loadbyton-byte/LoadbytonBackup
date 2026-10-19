from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.compliance import GST
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.distribution import Distribution
from leadengine.models import Signal
from leadengine.viewings import ViewingConflict, list_viewings, schedule, to_ics

T = datetime(2026, 3, 10, 10, 0, tzinfo=GST)
BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer"


def mk(pipe, n=1, name=None):
    return pipe.ingest(Signal(platform="reddit", external_id=f"s{n}", text=BUY, author_handle=f"person_{n}_x", author_name=name)).lead_id


def test_overlap_conflict_same_agent_only(pipe):
    d = Distribution(pipe.db)
    a, b = d.add_agent("A"), d.add_agent("B")
    l1, l2, l3 = mk(pipe, 1), mk(pipe, 2), mk(pipe, 3)
    schedule(pipe.db, l1, T, agent_id=a)
    with pytest.raises(ViewingConflict):
        schedule(pipe.db, l2, T + timedelta(minutes=15), agent_id=a)
    schedule(pipe.db, l2, T + timedelta(minutes=30), agent_id=a)                 # back-to-back is fine
    schedule(pipe.db, l3, T + timedelta(minutes=15), agent_id=b)                 # other agent is free


def test_schedule_sets_status_activity_and_defaults_to_assigned_agent(pipe):
    lid = mk(pipe)
    a = Distribution(pipe.db).add_agent("A")
    pipe.distribution.assign(lid)
    vid = schedule(pipe.db, lid, T, location="Marina Gate")
    v = pipe.db.one("SELECT * FROM viewings WHERE id=?", (vid,))
    assert v["agent_id"] == a and v["start_ts"] == "2026-03-10T06:00:00+00:00"        # stored in UTC
    assert pipe.db.one("SELECT status FROM leads WHERE id=?", (lid,))["status"] == "viewing"
    assert pipe.db.one("SELECT kind FROM activities WHERE kind='viewing_scheduled'")


def test_naive_datetime_is_uae_time(pipe):
    vid = schedule(pipe.db, mk(pipe), datetime(2026, 3, 10, 10, 0))
    assert pipe.db.one("SELECT start_ts FROM viewings WHERE id=?", (vid,))["start_ts"] == "2026-03-10T06:00:00+00:00"


@pytest.mark.parametrize("minutes", [0, 4, 481])
def test_invalid_duration(pipe, minutes):
    with pytest.raises(ValueError):
        schedule(pipe.db, mk(pipe), T, duration_min=minutes)


def test_unknown_lead(pipe):
    with pytest.raises(LookupError):
        schedule(pipe.db, 999, T)


def test_ics_structure_uid_and_utc(pipe):
    lid = mk(pipe, name="Aisha K")
    vid = schedule(pipe.db, lid, T, duration_min=45, location="Marina Gate")
    ics = to_ics(list_viewings(pipe.db))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20260310T060000Z\r\n" in ics and "DTEND:20260310T064500Z\r\n" in ics
    assert f"UID:viewing-{vid}@digitalburj-leads\r\n" in ics and "SUMMARY:Viewing: Aisha K\r\n" in ics
    assert f"UID:viewing-{vid}@digitalburj-leads" in to_ics(list_viewings(pipe.db))     # stable across calls


def test_ics_escaping_and_folding(pipe):
    lid = mk(pipe, name="Eve <script>alert(1)</script>, Jr; \\")
    schedule(pipe.db, lid, T, location="Marina, Tower; Gate\nline2 " + "x" * 200)
    ics = to_ics(list_viewings(pipe.db))
    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75
    flat = ics.replace("\r\n ", "")
    assert "Marina\\, Tower\; Gate\\nline2" in flat and "Jr\; \\\\" in flat and "\r" not in flat.replace("\r\n", "")
    assert flat.count("LOCATION:") == 1 and "\nline2" not in flat


def test_ics_folding_never_splits_multibyte_chars(pipe):
    lid = mk(pipe, name="خالد " * 30)
    schedule(pipe.db, lid, T)
    ics = to_ics(list_viewings(pipe.db))
    assert ics.encode().decode("utf-8") == ics and "خالد" in ics.replace("\r\n ", "")


@pytest.fixture()
def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key="k"), DB(":memory:")))


def test_api_schedule_conflict_and_ics(client):
    H = {"x-api-key": "k"}
    client.post("/demo/seed", headers=H)
    a = client.post("/agents", json={"name": "A"}, headers=H).json()["id"]
    body = {"start": "2026-03-10T10:00:00+04:00", "duration_min": 30, "location": "Marina Gate", "agent_id": a}
    assert client.post("/leads/1/viewings", json=body, headers=H).status_code == 200
    assert client.post("/leads/2/viewings", json={**body, "start": "2026-03-10T10:10:00+04:00"}, headers=H).status_code == 409
    assert client.post("/leads/9999/viewings", json=body, headers=H).status_code == 404
    assert client.post("/leads/2/viewings", json={**body, "duration_min": 1}, headers=H).status_code == 422
    assert client.get("/viewings.ics").status_code == 401
    r = client.get(f"/viewings.ics?agent_id={a}&key=k")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and "BEGIN:VEVENT" in r.text
    assert client.get("/viewings.ics?key=wrong").status_code == 401
    assert client.get("/viewings", headers=H).json()[0]["location"] == "Marina Gate"
