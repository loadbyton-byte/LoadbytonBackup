import json
import os
import sys

import pytest

from leadengine import cli
from leadengine.config import Settings
from leadengine.platform.app import create_platform_app
from leadengine.platform.maintenance import create_owner, maintain
from leadengine.platform.security import generate_key

from .conftest import PW
from .test_app_auth import SIG


def settings_for(tmp_path, **kw):
    return Settings(db_path=":memory:", control_db=str(tmp_path / "control.db"), data_dir=str(tmp_path / "data"), encryption_key=generate_key(),
                    base_url="http://testserver", dev_insecure_cookies=True, **kw)


def seed_workspace(s, name, overdue=False):
    app = create_platform_app(s, inline_mail=True)
    t = app.state.tenants.create_tenant(name)
    pipe = app.state.tenants.app_for(t.id).state.pipeline
    pipe.ingest_many([__import__("leadengine.models", fromlist=["Signal"]).Signal(platform="reddit", external_id=f"{name}1", source_type="post", text=SIG[0]["text"],
                                                                                   author_handle=f"p_{name}")])
    return app, t


def test_maintain_runs_for_every_workspace_and_isolates_failures(tmp_path):
    s = settings_for(tmp_path)
    app, a = seed_workspace(s, "alpha")
    _, b = seed_workspace(s, "beta")
    _, c = seed_workspace(s, "gamma")
    app.state.tenants.evict(a.id); app.state.tenants.evict(b.id); app.state.tenants.evict(c.id)
    with open(app.state.control.one("SELECT data_path FROM tenants WHERE id=?", (b.id,))["data_path"], "wb") as f:
        f.write(b"this is not a sqlite database" * 50)                       # corrupt workspace B
    results, errors = maintain(s)
    done = {r["workspace_id"] for r in results}
    assert done == {a.id, c.id}
    assert all({"escalate", "purge"} <= set(r) for r in results)
    assert len(errors) == 1 and errors[0]["workspace_id"] == b.id and errors[0]["error"]


def test_maintain_cli_exit_code_and_output(tmp_path, monkeypatch, capsys):
    s = settings_for(tmp_path)
    seed_workspace(s, "alpha")
    monkeypatch.setenv("CONTROL_DB", s.control_db); monkeypatch.setenv("DATA_DIR", s.data_dir); monkeypatch.setenv("ENCRYPTION_KEY", s.encryption_key)
    monkeypatch.setattr(sys, "argv", ["leadengine", "maintain"])
    cli.main()                                                              # returns normally: all workspaces ok
    out = json.loads(capsys.readouterr().out)
    assert out["workspaces"] == 1 and out["errors"] == []


def test_maintain_requires_platform_mode(monkeypatch):
    monkeypatch.delenv("CONTROL_DB", raising=False)
    monkeypatch.setattr(sys, "argv", ["leadengine", "maintain"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "CONTROL_DB" in str(e.value)


def test_create_owner_makes_a_verified_owner_who_can_sign_in(tmp_path):
    s = settings_for(tmp_path)
    uid, tid = create_owner(s, "ops@agency.ae", PW, "Ops", "Ops Realty")
    app = create_platform_app(s, inline_mail=True)
    r = app.state.accounts.login("ops@agency.ae", PW)
    assert r.status == "ok"
    info = app.state.accounts.resolve_session(r.session_token)
    assert info.role == "owner" and info.email_verified and info.tenant_name == "Ops Realty"
    with pytest.raises(ValueError):
        create_owner(s, "ops@agency.ae", PW, "Ops", "Again")               # already exists
    with pytest.raises(ValueError):
        create_owner(s, "x@agency.ae", "short", "X", "X")


def test_serve_uses_platform_app_only_when_control_db_set(tmp_path, monkeypatch):
    from leadengine.main import create
    monkeypatch.delenv("CONTROL_DB", raising=False)
    legacy = create()
    assert hasattr(legacy.state, "pipeline") and not hasattr(legacy.state, "accounts")
    monkeypatch.setenv("CONTROL_DB", str(tmp_path / "c.db")); monkeypatch.setenv("DATA_DIR", str(tmp_path / "d")); monkeypatch.setenv("ENCRYPTION_KEY", generate_key())
    plat = create()
    assert hasattr(plat.state, "accounts") and hasattr(plat.state, "tenants")


def test_startup_validation_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DB", str(tmp_path / "c.db")); monkeypatch.setenv("DATA_DIR", str(tmp_path / "d")); monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["leadengine", "serve"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "ENCRYPTION_KEY" in str(e.value) and "Traceback" not in str(e.value)
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "ENCRYPTION_KEY" in str(e.value)
    monkeypatch.setenv("ENCRYPTION_KEY", generate_key()); monkeypatch.setenv("SIGNUP_MODE", "sometimes")
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "SIGNUP_MODE" in str(e.value)
