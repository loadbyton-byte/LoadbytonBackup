import re

import pytest

from leadengine.config import Settings
from leadengine.platform import security
from leadengine.platform.control import ControlDB
from leadengine.platform.mailer import ConsoleMailer
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
    return AccountService(control, mailer, FakeTenants(control), Settings(db_path=":memory:", base_url="https://app.example"), now=clock)


def token_from(mail: dict) -> str:
    return re.search(r"token=([A-Za-z0-9_-]+)", mail["text"]).group(1)


PW = "correct horse battery"
