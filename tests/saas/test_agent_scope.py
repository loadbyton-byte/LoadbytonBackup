import pytest
from fastapi.testclient import TestClient

from .test_app_auth import SIG, link_token, login, make_app, signed_in


def _lead(i):
    return {**SIG[0], "external_id": f"s{i}", "author_handle": f"person_{i}_handle", "author_name": f"Lead{i}"}


@pytest.fixture()
def team(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    owner.post("/ingest/signals", json=[_lead(1), _lead(2)], headers=oh)
    for n in ("Ann", "Bob"):
        owner.post("/agents", json={"name": n}, headers=oh)
    tid = owner.get("/auth/me").json()["workspace"]["id"]
    db = app.state.tenants.db_for(tid)
    with db.tx() as c:
        c.execute("UPDATE leads SET assigned_agent_id=1 WHERE id=1")
        c.execute("UPDATE leads SET assigned_agent_id=2 WHERE id=2")

    def agent(email, agent_id):
        owner.post("/auth/invite", json={"email": email, "role": "agent", "agent_id": agent_id}, headers=oh)
        c = TestClient(app)
        c.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "Ag"})
        return c, login(c, email)
    return app, owner, oh, agent


def test_agent_sees_only_assigned_leads(team):
    _, _, _, agent = team
    c, _ = agent("ann@a.ae", 1)
    assert [x["id"] for x in c.get("/leads").json()] == [1]
    assert [x["id"] for x in c.get("/leads?assigned_agent_id=2").json()] == [1]            # override attempt ignored
    assert [x["id"] for x in c.get("/leads?assigned_agent_id=2&assigned_agent_id=1&agent_id=2").json()] == [1]


def test_agent_cannot_open_unassigned_lead_or_its_subroutes(team):
    _, _, _, agent = team
    c, h = agent("ann@a.ae", 1)
    assert c.get("/leads/1").status_code == 200
    for p in ("/leads/2", "/leads/2/matches", "/leads/2/next-actions", "/leads/2/deal-sheet", "/leads/999"):
        assert c.get(p).status_code == 403, p
    for p, body in (("/leads/2/status", {"status": "won"}), ("/leads/2/activities", {"kind": "note", "note": "x"}),
                    ("/leads/2/viewings", {"start": "2030-03-10T10:00:00+04:00"}), ("/leads/2/outreach", {})):
        assert c.post(p, json=body, headers=h).status_code == 403, p


def test_agent_can_update_own_lead_status_but_not_reassign(team):
    app, owner, oh, agent = team
    c, h = agent("ann@a.ae", 1)
    assert c.post("/leads/1/status", json={"status": "contacted", "assigned_to": "Bob"}, headers=h).status_code == 200
    lead = owner.get("/leads/1").json()
    assert lead["status"] == "contacted" and lead.get("assigned_to") != "Bob"


def test_agent_viewings_and_tasks_are_own_only(team):
    _, owner, oh, agent = team
    owner.post("/leads/2/viewings", json={"start": "2030-03-10T10:00:00+04:00", "agent_id": 2, "location": "BOB-LOC"}, headers=oh)
    c, h = agent("ann@a.ae", 1)
    assert "BOB-LOC" not in c.get("/viewings").text and "BOB-LOC" not in c.get("/viewings?agent_id=2").text
    assert all(t["lead_id"] == 1 for t in c.get("/tasks/today?agent_id=2").json())
    assert c.post("/leads/1/viewings", json={"start": "2030-03-11T10:00:00+04:00", "agent_id": 2}, headers=h).status_code == 200
    assert {v["agent_id"] for v in owner.get("/viewings").json() if v["lead_id"] == 1} == {1}      # forced to own agent id


def test_agent_without_linked_agent_sees_nothing(team):
    _, owner, oh, agent = team
    c, _ = agent("nolink@a.ae", None)
    assert c.get("/leads").json() == [] and c.get("/tasks/today").json() == [] and c.get("/viewings").json() == []
    assert c.get("/leads/1").status_code == 403


def test_manager_unaffected(team):
    app, owner, oh, _ = team
    owner.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=oh)
    from .test_app_auth import link_token as lt
    m = TestClient(app)
    m.post("/auth/invite/accept", json={"token": lt(app.state.accounts.mailer, "invite"), "password": "correct horse battery", "name": "M"})
    login(m, "m@a.ae")
    assert len(m.get("/leads").json()) == 2 and m.get("/leads/2").status_code == 200
