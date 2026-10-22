import pytest

from .test_agent_scope import team  # noqa: F401
from .test_app_auth import make_app, signed_in


def mk(owner, oh, lead, agent_id, value=1_000_000):
    r = owner.post("/deals", json={"lead_id": lead, "agent_id": agent_id, "kind": "sale", "value_aed": value}, headers=oh)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_agent_sees_and_touches_only_own_deals(team):  # noqa: F811
    app, owner, oh, agent = team
    d1, d2 = mk(owner, oh, 1, 1), mk(owner, oh, 2, 2, 3_000_000)
    c, h = agent("ann@a.ae", 1)
    assert [d["id"] for d in c.get("/deals").json()] == [d1]
    assert [d["id"] for d in c.get("/deals?agent_id=2").json()] == [d1]                      # override attempt ignored
    assert c.get(f"/deals/{d1}").status_code == 200
    for method, path, body in (("get", f"/deals/{d2}", None), ("patch", f"/deals/{d2}", {"value_aed": 1}), ("post", f"/deals/{d2}/stage", {"stage": "paid"})):
        r = getattr(c, method)(path, **({"json": body, "headers": h} if body is not None else {}))
        assert r.status_code == 403, (method, path)
    assert c.get("/deals/999").status_code == 403                                           # nonexistent looks the same as someone else's
    assert owner.get(f"/deals/{d2}").json()["value_aed"] == 3_000_000


def test_agent_summary_and_leaderboard_show_only_their_row(team):  # noqa: F811
    app, owner, oh, agent = team
    for lead, aid, value in ((1, 1, 1_000_000), (2, 2, 3_000_000)):
        owner.post(f"/deals/{mk(owner, oh, lead, aid, value)}/stage", json={"stage": "paid"}, headers=oh)
    c, _ = agent("ann@a.ae", 1)
    assert [r["agent_id"] for r in c.get("/deals/leaderboard").json()] == [1]
    assert [r["agent_id"] for r in c.get("/deals/leaderboard?agent_id=2").json()] == [1]
    assert c.get("/deals/summary").json()["closed_this_month"]["value_aed"] == 1_000_000
    assert [r["agent_id"] for r in owner.get("/deals/leaderboard").json()] == [2, 1]


def test_agent_creates_deals_only_for_their_leads_and_as_themselves(team):  # noqa: F811
    app, owner, oh, agent = team
    c, h = agent("ann@a.ae", 1)
    assert c.post("/deals", json={"lead_id": 2, "kind": "sale", "value_aed": 1_000_000}, headers=h).status_code == 403
    r = c.post("/deals", json={"lead_id": 1, "agent_id": 2, "kind": "sale", "value_aed": 1_000_000}, headers=h)
    assert r.status_code == 200 and r.json()["agent_id"] == 1                               # cannot book it for Bob
    did = r.json()["id"]
    assert c.patch(f"/deals/{did}", json={"agent_id": 2, "value_aed": 2_000_000}, headers=h).json()["agent_id"] == 1
    assert c.post("/deals", content="[]", headers={**h, "content-type": "application/json"}).status_code == 400


def test_settings_and_isolation(tmp_path, team):  # noqa: F811
    app, owner, oh, agent = team
    c, h = agent("ann@a.ae", 1)
    assert c.put("/deals/settings", json={"vat_pct": 5}, headers=h).status_code == 403 and c.get("/deals/settings").status_code == 403
    assert owner.put("/deals/settings", json={"commission_sale_pct": 3}, headers=oh).status_code == 200
    other, oth = signed_in(app, app.state.accounts.mailer, "z@z.ae", "Zed")
    assert other.get("/deals").json() == [] and other.get("/deals/settings").json()["commission_sale_pct"] == 2
