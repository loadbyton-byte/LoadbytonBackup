import re

import pytest

from leadengine.config import Settings
from leadengine.platform import security
from leadengine.platform.control import ControlDB
from leadengine.platform.mailer import ConsoleMailer
from leadengine.platform.security import Encryptor, generate_key
from leadengine.platform.tenancy import Tenant


@pytest.fixture(autouse=True)
def fast_scrypt(request, monkeypatch):
    """Cheap scrypt for most tests; test_security keeps production parameters."""
    if not request.module.__name__.endswith("test_security"):
        monkeypatch.setattr(security, "SCRYPT_N", 2**10)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeTenants:
    """Minimal stand-in for TenantManager.create_tenant until the real manager exists (Task 6)."""

    def __init__(self, control: ControlDB):
        self.control, self.n = control, 0

    def create_tenant(self, name: str) -> Tenant:
        self.n += 1
        key = f"pk_fake{self.n:04d}"
        with self.control.tx() as c:
            tid = c.execute("INSERT INTO tenants(name,public_key,data_path,status,created_at) VALUES(?,?,?,?,?)",
                            (name, key, f"/tmp/fake{self.n}.db", "active", "2026-01-01")).lastrowid
        return Tenant(tid, name, key)


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def control():
    return ControlDB(":memory:")


@pytest.fixture()
def mailer():
    return ConsoleMailer()


@pytest.fixture()
def accounts(control, mailer, clock):
    from leadengine.platform.accounts import AccountService
    return AccountService(control, mailer, FakeTenants(control), Settings(db_path=":memory:", base_url="https://app.example"), now=clock,
                          encryptor=Encryptor(generate_key()))


def token_from(mail: dict) -> str:
    return re.search(r"token=([A-Za-z0-9_-]+)", mail["text"]).group(1)


PW = "correct horse battery"


@pytest.fixture(autouse=True)
def fast_floors(monkeypatch):
    from leadengine.platform import app as appmod
    monkeypatch.setattr(appmod, "REGISTER_FLOOR_S", 0.03)
    monkeypatch.setattr(appmod, "FORGOT_FLOOR_S", 0.03)


# ---------------------------------------------------------------- live server + browser fixtures (skip when Playwright/Chromium is missing)
class Live:
    def __init__(self, url, app, mailer):
        self.url, self.app, self.mailer = url, app, mailer


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    import socket
    import threading
    import time as _t

    import uvicorn

    from leadengine.platform.app import create_platform_app
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    url = f"http://127.0.0.1:{port}"
    settings = Settings(db_path=":memory:", control_db=":memory:", data_dir=str(tmp_path_factory.mktemp("live")), encryption_key=generate_key(),
                        base_url=url, dev_insecure_cookies=True, contact_email="hello@example.ae")
    mailer = ConsoleMailer()
    from leadengine.platform import accounts as accounts_mod
    old_limit, accounts_mod.REGISTER_LIMIT = accounts_mod.REGISTER_LIMIT, 10_000     # many workspaces are created from one address
    app = create_platform_app(settings, mailer, inline_mail=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        _t.sleep(0.05)
    yield Live(url, app, mailer)
    server.should_exit = True
    accounts_mod.REGISTER_LIMIT = old_limit


@pytest.fixture()
def browser_page():
    import os
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium"
        try:
            browser = p.chromium.launch(executable_path=exe) if os.path.exists(exe) else p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")

        def make(width=1280, height=900, locale="en-US"):
            ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale)
            pg = ctx.new_page()
            pg.problems = []
            pg.on("console", lambda m: pg.problems.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: pg.problems.append(str(e)))
            pg.on("response", lambda r: pg.problems.append(f"{r.status} {r.url}") if r.status >= 400 else None)
            return pg
        yield make
        browser.close()
