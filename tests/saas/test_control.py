import json
import sqlite3

import pytest

from leadengine.platform.control import ControlDB


@pytest.fixture()
def cdb():
    return ControlDB(":memory:")


def mk_user(c, email="a@x.com"):
    with c.tx() as cn:
        return cn.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)", (email, "h", "2026-01-01")).lastrowid


def mk_tenant(c, name="T", key="pk_1"):
    with c.tx() as cn:
        return cn.execute("INSERT INTO tenants(name,public_key,data_path,status,created_at) VALUES(?,?,?,?,?)",
                          (name, key, "/tmp/x.db", "active", "2026-01-01")).lastrowid


def test_schema_created_idempotently(tmp_path):
    p = str(tmp_path / "c.db")
    ControlDB(p)
    c = ControlDB(p)
    names = {r["name"] for r in c.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "tenants", "memberships", "sessions", "invites", "auth_tokens", "recovery_codes", "login_attempts",
            "tenant_settings", "audit"} <= names


def test_email_unique_case_insensitive(cdb):
    mk_user(cdb, "A@x.com")
    with pytest.raises(sqlite3.IntegrityError):
        mk_user(cdb, "a@X.COM")


def test_public_key_unique(cdb):
    mk_tenant(cdb, key="pk_same")
    with pytest.raises(sqlite3.IntegrityError):
        mk_tenant(cdb, "T2", "pk_same")


def test_deleting_a_tenant_cascades(cdb):
    u, t = mk_user(cdb), mk_tenant(cdb)
    with cdb.tx() as c:
        c.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(?,?,?)", (u, t, "owner"))
        c.execute("INSERT INTO tenant_settings(tenant_id,key,value_enc) VALUES(?,?,?)", (t, "brn", "x"))
        c.execute("INSERT INTO invites(token_hash,tenant_id,email,role,expires_at) VALUES(?,?,?,?,?)", ("h", t, "e@x.com", "agent", "2030"))
        c.execute("INSERT INTO sessions(token_hash,user_id,tenant_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?)", ("s", u, t, 1, 1, 9))
        c.execute("DELETE FROM tenants WHERE id=?", (t,))
    for table in ("memberships", "tenant_settings", "invites", "sessions"):
        assert cdb.one(f"SELECT COUNT(*) n FROM {table}")["n"] == 0
    assert cdb.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_deleting_a_user_cascades(cdb):
    u, t = mk_user(cdb), mk_tenant(cdb)
    with cdb.tx() as c:
        c.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(?,?,?)", (u, t, "owner"))
        c.execute("INSERT INTO sessions(token_hash,user_id,tenant_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?)", ("s", u, t, 1, 1, 9))
        c.execute("INSERT INTO auth_tokens(token_hash,user_id,kind,expires_at) VALUES(?,?,?,?)", ("a", u, "verify", 9))
        c.execute("INSERT INTO recovery_codes(user_id,code_hash) VALUES(?,?)", (u, "c"))
        c.execute("DELETE FROM users WHERE id=?", (u,))
    for table in ("memberships", "sessions", "auth_tokens", "recovery_codes"):
        assert cdb.one(f"SELECT COUNT(*) n FROM {table}")["n"] == 0
    assert cdb.one("SELECT COUNT(*) n FROM tenants")["n"] == 1


def test_role_check_constraint(cdb):
    u, t = mk_user(cdb), mk_tenant(cdb)
    with pytest.raises(sqlite3.IntegrityError):
        with cdb.tx() as c:
            c.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(?,?,?)", (u, t, "god"))


def test_audit_records_detail_and_refuses_secret_like_keys(cdb):
    cdb.audit("login", user_id=1, tenant_id=2, ip="1.2.3.4", method="password")
    row = cdb.one("SELECT * FROM audit")
    assert (row["action"], row["user_id"], row["tenant_id"], row["ip"]) == ("login", 1, 2, "1.2.3.4") and json.loads(row["detail"]) == {"method": "password"}
    for bad in ("password", "token", "secret", "code", "api_key", "reset_token"):
        with pytest.raises(ValueError):
            cdb.audit("x", **{bad: "v"})
    assert cdb.one("SELECT COUNT(*) n FROM audit")["n"] == 1
