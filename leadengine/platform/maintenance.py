"""Operator tasks for platform mode: scheduled maintenance across every workspace and first-owner bootstrap."""
from __future__ import annotations

from ..config import Settings
from .accounts import AccountError, AccountService
from .control import ControlDB
from .mailer import ConsoleMailer
from .security import Encryptor
from .tenancy import TenantManager


def _manager(s: Settings) -> tuple[ControlDB, TenantManager]:
    if not s.control_db:
        raise ValueError("CONTROL_DB is required for platform mode")
    if not s.encryption_key:
        raise ValueError("ENCRYPTION_KEY is required for platform mode")
    control = ControlDB(s.control_db)
    return control, TenantManager(control, s.data_dir, s, Encryptor(s.encryption_key))


def maintain(s: Settings) -> tuple[list[dict], list[dict]]:
    """SLA escalation + retention purge for every active workspace. One broken workspace never stops the others."""
    control, tenants = _manager(s)
    results, errors = [], []
    for row in control.q("SELECT id, name FROM tenants WHERE status='active' ORDER BY id"):
        try:
            with tenants.lease(row["id"]) as app:
                pipe = app.state.pipeline
                results.append({"workspace_id": row["id"], "name": row["name"], "escalate": pipe.distribution.escalate_overdue(),
                                "purge": pipe.compliance.purge_expired(), "followups": app.state.followups.tick()})
        except Exception as e:                                         # isolate: report and continue
            errors.append({"workspace_id": row["id"], "name": row["name"], "error": f"{type(e).__name__}: {e}"[:300]})
    return results, errors


def create_owner(s: Settings, email: str, password: str, name: str, agency: str) -> tuple[int, int]:
    """Create a verified owner and its workspace (operator bootstrap; works with sign-up closed)."""
    control, tenants = _manager(s)
    svc = AccountService(control, ConsoleMailer(), tenants, s, encryptor=Encryptor(s.encryption_key))
    try:
        r = svc.register(email, password, name, agency, ip="operator")
    except AccountError as e:
        raise ValueError(e.message) from e
    if r.user_id == 0:
        raise ValueError("An account with that email already exists.")
    with control.tx() as c:
        c.execute("UPDATE users SET email_verified_at=datetime('now') WHERE id=?", (r.user_id,))
    return r.user_id, r.tenant_id
