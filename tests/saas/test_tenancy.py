import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from leadengine.config import Settings
from leadengine.platform.control import ControlDB
from leadengine.platform.security import Encryptor, generate_key
from leadengine.platform.tenancy import TenantManager

SIG = [{"platform": "reddit", "external_id": "x1", "text": "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, this week", "author_handle": "person_one"}]


@pytest.fixture()
def mgr(tmp_path):
    base = Settings(db_path=":memory:", api_key="master", base_url="https://app.example")
    return TenantManager(ControlDB(":memory:"), str(tmp_path), base, Encryptor(generate_key()))


def test_create_tenant_makes_isolated_file_and_unique_keys(mgr, tmp_path):
    a, b = mgr.create_tenant("Acme"), mgr.create_tenant("Beta")
    assert a.id != b.id and a.public_key != b.public_key and a.public_key.startswith("pk_") and len(a.public_key) >= 25
    paths = {r["data_path"] for r in mgr.control.q("SELECT data_path FROM tenants")}
    assert len(paths) == 2 and all(os.path.exists(p) and p.startswith(str(tmp_path)) for p in paths)
    assert oct(os.stat(tmp_path / "tenants").st_mode & 0o777) == oct(0o700)


def test_two_tenants_have_independent_data(mgr):
    a, b = mgr.create_tenant("Acme"), mgr.create_tenant("Beta")
    ca, cb = TestClient(mgr.app_for(a.id)), TestClient(mgr.app_for(b.id))
    assert ca.post("/ingest/signals", json=SIG).json()["created"] == 1
    assert cb.post("/ingest/signals", json=SIG).json()["created"] == 1           # same external_id is fine: separate database
    ca.post("/agents", json={"name": "OnlyInAcme"})
    assert [x["name"] for x in ca.get("/agents").json()] == ["OnlyInAcme"] and cb.get("/agents").json() == []
    assert ca.get("/leads").json()[0]["id"] == 1 == cb.get("/leads").json()[0]["id"]


def test_tenant_app_does_not_use_the_platform_api_key(mgr):
    t = mgr.create_tenant("Acme")
    assert mgr.settings_for(t.id).api_key == "" and TestClient(mgr.app_for(t.id)).get("/leads").status_code == 200


def test_settings_overlay_encrypts_and_roundtrips(mgr):
    t = mgr.create_tenant("Acme")
    mgr.set_setting(t.id, "meta_app_secret", "s3cret-value")
    mgr.set_setting(t.id, "brn", "12345")
    mgr.set_setting(t.id, "capture_origins", ["https://acme.ae"])
    mgr.set_setting(t.id, "contact_hour_end", 21)
    raw = [r["value_enc"] for r in mgr.control.q("SELECT value_enc FROM tenant_settings")]
    assert raw and all("s3cret-value" not in v and "12345" not in v for v in raw)
    s = mgr.settings_for(t.id)
    assert (s.meta_app_secret, s.brn, s.capture_origins, s.contact_hour_end) == ("s3cret-value", "12345", ["https://acme.ae"], 21)
    assert s.public_url == f"https://app.example/t/{t.public_key}" and s.db_path.endswith(f"{t.id}.db")
    mgr.set_setting(t.id, "brn", "999")
    assert mgr.settings_for(t.id).brn == "999"


def test_unknown_or_badly_typed_setting_rejected(mgr):
    t = mgr.create_tenant("Acme")
    for key, val in (("api_key", "x"), ("db_path", "/etc/passwd"), ("control_db", "x"), ("nonsense", 1), ("contact_hour_end", "late"),
                     ("capture_origins", "not-a-list"), ("brn", ["list"])):
        with pytest.raises(ValueError):
            mgr.set_setting(t.id, key, val)
    assert mgr.control.one("SELECT COUNT(*) n FROM tenant_settings")["n"] == 0


def test_settings_with_wrong_encryption_key_fail_loudly(mgr, tmp_path):
    t = mgr.create_tenant("Acme")
    mgr.set_setting(t.id, "brn", "12345")
    other = TenantManager(mgr.control, str(tmp_path), Settings(db_path=":memory:"), Encryptor(generate_key()))
    with pytest.raises(ValueError):
        other.settings_for(t.id)


def test_overlay_applies_to_the_running_app_after_restart_of_runtime(mgr):
    t = mgr.create_tenant("Acme")
    mgr.set_setting(t.id, "agency_name", "Acme Realty LLC")
    mgr.evict(t.id)
    assert mgr.app_for(t.id).state.settings.agency_name == "Acme Realty LLC"


def test_lru_eviction_keeps_in_flight_lease_open(tmp_path):
    m = TenantManager(ControlDB(":memory:"), str(tmp_path), Settings(db_path=":memory:"), Encryptor(generate_key()), max_open=1)
    a, b = m.create_tenant("A"), m.create_tenant("B")
    db_a = m.db_for(a.id)
    with m.lease(a.id) as app_a:
        m.app_for(b.id)                                                    # pushes A out of the cache while it is leased
        assert TestClient(app_a).get("/health").json()["ok"] is True       # still usable
        db_a.conn.execute("select 1")
    with pytest.raises(sqlite3.ProgrammingError):                          # closed once the lease ended
        db_a.conn.execute("select 1")


def test_deleted_tenant_is_never_served(mgr):
    a = mgr.create_tenant("Acme")
    path = mgr.control.one("SELECT data_path FROM tenants WHERE id=?", (a.id,))["data_path"]
    app = mgr.app_for(a.id)
    TestClient(app).post("/ingest/signals", json=SIG)
    mgr.delete_tenant(a.id)
    assert not os.path.exists(path) and mgr.tenant_by_key(a.public_key) is None
    with pytest.raises(KeyError):
        mgr.app_for(a.id)
    with pytest.raises(KeyError):
        with mgr.lease(a.id):
            pass
    assert any(r["action"] == "tenant_deleted" for r in mgr.control.q("SELECT action FROM audit"))


def test_tenant_by_key(mgr):
    a = mgr.create_tenant("Acme")
    assert mgr.tenant_by_key(a.public_key).id == a.id
    for bad in ("", "pk_nope", a.public_key + "x", a.public_key.upper(), None, "x" * 500):
        assert mgr.tenant_by_key(bad) is None


def test_create_tenant_rolls_back_on_failure(tmp_path, monkeypatch):
    m = TenantManager(ControlDB(":memory:"), str(tmp_path), Settings(db_path=":memory:"), Encryptor(generate_key()))
    import leadengine.platform.tenancy as t
    monkeypatch.setattr(t, "DB", lambda path: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        m.create_tenant("Acme")
    assert m.control.one("SELECT COUNT(*) n FROM tenants")["n"] == 0
