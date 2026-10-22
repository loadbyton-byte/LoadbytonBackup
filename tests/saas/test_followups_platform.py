from fastapi.testclient import TestClient

from leadengine.platform.maintenance import maintain

from .test_app_auth import link_token, login, make_app, signed_in
from .test_markets_api import agent_client

FORM = {"name": "Sara Buyer", "phone": "0501234567", "email": "sara@example.com", "consent": True, "area": "Dubai Marina", "looking_to": "buy"}


def key_of(c):
    return c.get("/auth/me").json()["workspace"]["public_key"]


def test_public_capture_enrols_and_manager_approves(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    anon = TestClient(app)
    assert anon.post(f"/t/{key_of(c)}/capture", json=FORM).status_code == 200
    assert c.post("/maintenance/followups", headers=h).json()["queued"] == 1
    q = c.get("/followups/queue").json()
    assert len(q) == 1
    assert c.put("/followups/settings", json={"autosend": True}, headers=h).json()["autosend"] is True
    assert c.post(f"/followups/{q[0]['id']}/approve", json={}, headers=h).status_code == 200


def test_roles_and_isolation(tmp_path):
    app, mailer = make_app(tmp_path)
    a, ah = signed_in(app, mailer, "a@a.ae", "Alpha")
    b, bh = signed_in(app, mailer, "b@b.ae", "Beta")
    TestClient(app).post(f"/t/{key_of(a)}/capture", json=FORM)
    a.post("/maintenance/followups", headers=ah)
    assert len(a.get("/followups/queue").json()) == 1 and b.get("/followups/queue").json() == []
    ag, agh = agent_client(app, mailer, a, ah)
    assert ag.get("/followups/queue").status_code == 403 and ag.put("/followups/settings", json={"autosend": True}, headers=agh).status_code == 403
    assert TestClient(app).get("/followups/queue").status_code == 401
    assert TestClient(app).get(f"/t/{key_of(a)}/followups/queue").status_code == 404


def test_maintain_runs_the_worker_for_every_workspace(tmp_path):
    from .test_cli_maintain import settings_for
    from leadengine.platform.app import create_platform_app
    st = settings_for(tmp_path)
    app = create_platform_app(st, inline_mail=True)
    t = app.state.tenants.create_tenant("Alpha")
    TestClient(app).post(f"/t/{t.public_key}/capture", json=FORM)
    results, errors = maintain(st)
    assert errors == [] and results[0]["followups"]["queued"] == 1


def test_unsubscribe_link_works_through_the_public_workspace_path(tmp_path):
    app, mailer = make_app(tmp_path)
    c, h = signed_in(app, mailer)
    anon = TestClient(app)
    anon.post(f"/t/{key_of(c)}/capture", json=FORM)
    lead = c.get("/leads").json()[0]["id"]
    tenant = app.state.tenants.app_for(c.get("/auth/me").json()["workspace"]["id"])
    url = tenant.state.followups.unsubscribe_url(lead)
    assert url.startswith(f"http://testserver/t/{key_of(c)}/unsubscribe?t=")
    path = url.replace("http://testserver", "")
    assert anon.get(path).status_code == 200 and c.get("/leads").json() != []
    assert anon.post(path).status_code == 200 and c.get("/leads").json() == []
    assert anon.get(f"/t/{key_of(c)}/unsubscribe?t=bad").status_code == 404
