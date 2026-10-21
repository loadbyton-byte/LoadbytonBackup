import json

from fastapi.testclient import TestClient

from .test_app_auth import SIG, link_token, login, make_app, signed_in


def lead(i, **kw):
    return {**SIG[0], "external_id": f"mk{i}", "author_handle": f"person_{i}_mk", "author_name": f"Zed Person {i}", "phone": kw.pop("phone", "+447911123456"), **kw}


def agent_client(app, mailer, owner, oh):
    owner.post("/auth/invite", json={"email": "ag@a.ae", "role": "agent"}, headers=oh)
    a = TestClient(app)
    a.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "Ag"})
    return a, login(a, "ag@a.ae")


def test_roles_on_intel_and_backfill(tmp_path):
    app, mailer = make_app(tmp_path)
    owner, oh = signed_in(app, mailer)
    owner.post("/ingest/signals", json=[lead(1)], headers=oh)
    assert owner.get("/markets/intel").status_code == 200
    assert owner.get("/markets/intel/GB").status_code == 200
    assert owner.post("/markets/backfill-origin", headers=oh).status_code == 200
    key = owner.post("/auth/api-key", headers=oh).json()["api_key"]
    machine = TestClient(app)
    assert machine.get("/markets/intel", headers={"X-API-Key": key}).status_code == 200          # manager rights
    assert machine.post("/markets/backfill-origin", headers={"X-API-Key": key}).status_code == 403
    owner.post("/auth/invite", json={"email": "m@a.ae", "role": "manager"}, headers=oh)
    m = TestClient(app)
    m.post("/auth/invite/accept", json={"token": link_token(mailer, "invite"), "password": "correct horse battery", "name": "M"})
    mh = login(m, "m@a.ae")
    assert m.get("/markets/intel").status_code == 200 and m.post("/markets/backfill-origin", headers=mh).status_code == 403
    a, ah = agent_client(app, mailer, owner, oh)
    assert a.get("/markets/intel").status_code == 403 and a.post("/markets/backfill-origin", headers=ah).status_code == 403
    assert TestClient(app).get("/markets/intel").status_code == 401


def test_report_is_per_workspace_and_has_no_personal_data(tmp_path):
    app, mailer = make_app(tmp_path)
    a, ah = signed_in(app, mailer, "a@a.ae", "Alpha")
    b, bh = signed_in(app, mailer, "b@b.ae", "Beta")
    a.post("/ingest/signals", json=[lead(1, phone="+447911123456")], headers=ah)
    b.post("/ingest/signals", json=[lead(2, phone="+919876543210")], headers=bh)
    ra, rb = a.get("/markets/intel"), b.get("/markets/intel")
    assert [c["id"] for c in ra.json()["countries"]] == ["GB"] and [c["id"] for c in rb.json()["countries"]] == ["IN"]
    assert "Zed Person" not in ra.text and "447911" not in ra.text and "919876" not in rb.text
