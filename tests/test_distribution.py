import sqlite3
from datetime import datetime, timedelta, timezone

from leadengine.db import DB
from leadengine.distribution import Distribution
from leadengine.models import Signal

EN = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"
AR = "ابغى اشتري شقه غرفتين وصاله في دبي مارينا ميزانيتي 2 مليون ومستعجل"


def lead(pipe, n, text=EN):
    r = pipe.ingest(Signal(platform="reddit", external_id=f"s{n}", text=text, author_handle=f"person_{n}_x"))
    return r.lead_id


def test_prefers_area_and_language_match(pipe):
    ar_lead = lead(pipe, 1, AR)
    d = Distribution(pipe.db)
    en_agent = d.add_agent("Eli", languages=("en",))
    ar_agent = d.add_agent("Layla", areas=("Dubai Marina",), languages=("en", "ar"))
    assert d.assign(ar_lead) == ar_agent != en_agent


def test_weighted_round_robin(pipe):
    ids = [lead(pipe, i) for i in range(6)]
    d = Distribution(pipe.db)
    a = d.add_agent("A", weight=2)
    b = d.add_agent("B", weight=1)
    got = [d.assign(i) for i in ids]
    assert got.count(a) == 4 and got.count(b) == 2


def test_capacity_respected_and_no_agents(pipe):
    l1, l2 = lead(pipe, 1), lead(pipe, 2)
    d = Distribution(pipe.db)
    assert d.assign(l1) is None                       # zero agents: no exception
    a = d.add_agent("A", max_open=1)
    assert d.assign(l1) == a
    assert d.assign(l2) is None                       # everyone at max_open
    assert pipe.db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (l2,))["assigned_agent_id"] is None


def test_pipeline_auto_assigns_hot_leads(pipe):
    a = pipe.distribution.add_agent("A")
    lid = lead(pipe, 1)
    assert pipe.db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (lid,))["assigned_agent_id"] == a


def test_escalation_reassigns_then_flags(pipe):
    lid = lead(pipe, 1)
    d = Distribution(pipe.db)
    for n in "ABC":
        d.add_agent(n)
    first = d.assign(lid)
    t = datetime.now(timezone.utc)
    r1 = d.escalate_overdue(t + timedelta(days=1))
    row = pipe.db.one("SELECT assigned_agent_id, escalation_level FROM leads WHERE id=?", (lid,))
    assert r1 == {"reassigned": 1, "flagged": 0} and row["assigned_agent_id"] != first and row["escalation_level"] == 1
    r2 = d.escalate_overdue(t + timedelta(days=2))
    assert r2["reassigned"] == 1 and pipe.db.one("SELECT escalation_level FROM leads WHERE id=?", (lid,))["escalation_level"] == 2
    holder = pipe.db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (lid,))["assigned_agent_id"]
    r3 = d.escalate_overdue(t + timedelta(days=3))
    assert r3 == {"reassigned": 0, "flagged": 1}
    assert pipe.db.one("SELECT assigned_agent_id FROM leads WHERE id=?", (lid,))["assigned_agent_id"] == holder


def test_escalation_with_no_agents_flags_without_crash(pipe):
    lead(pipe, 1)
    r = Distribution(pipe.db).escalate_overdue(datetime.now(timezone.utc) + timedelta(days=1))
    assert r == {"reassigned": 0, "flagged": 1}


def test_contacted_leads_are_not_escalated(pipe):
    lid = lead(pipe, 1)
    d = Distribution(pipe.db)
    d.add_agent("A"); d.add_agent("B")
    d.assign(lid)
    with pipe.db.tx() as c:
        c.execute("UPDATE leads SET status='contacted' WHERE id=?", (lid,))
    assert d.escalate_overdue(datetime.now(timezone.utc) + timedelta(days=1)) == {"reassigned": 0, "flagged": 0}


def test_migration_adds_columns_to_old_db(tmp_path):
    p = str(tmp_path / "old.db")
    db = DB(p)
    db.conn.execute("ALTER TABLE leads DROP COLUMN assigned_agent_id")
    db.conn.execute("ALTER TABLE leads DROP COLUMN escalation_level")
    db.conn.commit(); db.conn.close()
    cols = {r[1] for r in DB(p).conn.execute("PRAGMA table_info(leads)")}
    assert {"assigned_agent_id", "escalation_level"} <= cols


def test_agents_api_roundtrip():
    from fastapi.testclient import TestClient

    from leadengine.api import create_app
    from leadengine.config import Settings

    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    c.post("/demo/seed")
    a = c.post("/agents", json={"name": "Layla", "languages": ["en", "ar"], "areas": ["Dubai Marina"], "weight": 2}).json()
    assert c.get("/agents").json()[0]["name"] == "Layla" and c.get("/agents").json()[0]["open_leads"] == 0
    assert c.patch(f"/agents/{a['id']}", json={"active": False, "max_open": 3}).json()["active"] is False
    assert c.post("/leads/1/assign").json() == {"assigned_agent_id": None}        # only agent is inactive
    c.patch(f"/agents/{a['id']}", json={"active": True})
    assert c.post("/leads/1/assign").json()["assigned_agent_id"] == a["id"]
    assert set(c.post("/maintenance/escalate").json()) == {"reassigned", "flagged"}
    assert c.post("/leads/9999/assign").status_code == 404
    assert c.patch("/agents/9999", json={"active": True}).status_code == 404
