"""Workspace management: one SQLite file and one app instance per workspace.

Isolation is structural: each workspace's `create_app` closes over its own `DB`, so a request that has been routed to a workspace
cannot read another workspace's rows no matter what ids it guesses. The manager keeps an LRU of live apps; an evicted app stays open
until every in-flight request (lease) that holds it has finished."""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from ..api import create_app
from ..config import Settings
from ..db import DB
from .control import ControlDB
from .security import Encryptor

# key -> (python type, is_secret). Only these can be overlaid per workspace; anything else (api_key, db_path, control_db...) is refused.
SETTING_KEYS: dict[str, tuple[type, bool]] = {
    "agency_name": (str, False), "agent_name": (str, False), "brn": (str, False), "trakheesi": (str, False),
    "capture_origins": (list, False), "contact_hour_start": (int, False), "contact_hour_end": (int, False),
    "whatsapp_phone_id": (str, False), "meta_verify_token": (str, True), "meta_app_secret": (str, True),
    "meta_page_token": (str, True), "whatsapp_token": (str, True), "form_webhook_secret": (str, True),
}


_BLANK: dict = {
    "agent_name": "", "brn": "", "trakheesi": "", "capture_origins": [], "dnc_file": "", "whatsapp_token": "", "whatsapp_phone_id": "",
    "whatsapp_send_enabled": False, "meta_app_secret": "", "meta_verify_token": "", "meta_page_token": "", "form_webhook_secret": "",
    "reddit_client_id": "", "reddit_client_secret": "", "apify_token": "",
}


@dataclass(frozen=True)
class Tenant:
    id: int
    name: str
    public_key: str


class _Runtime:
    def __init__(self, app, db: DB):
        self.app, self.db, self.refs, self.evicted = app, db, 0, False


class TenantManager:
    def __init__(self, control: ControlDB, data_dir: str, base_settings: Settings, encryptor: Encryptor, max_open: int = 50):
        self.control, self.base, self.enc, self.max_open = control, base_settings, encryptor, max_open
        self.dir = os.path.join(data_dir, "tenants")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._cache: OrderedDict[int, _Runtime] = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def create_tenant(self, name: str) -> Tenant:
        key = "pk_" + secrets.token_urlsafe(16)
        with self.control.tx() as c:
            tid = c.execute("INSERT INTO tenants(name,public_key,data_path,status,created_at) VALUES(?,?,?,?,datetime('now'))",
                            (name, key, "", "active")).lastrowid
            path = os.path.join(self.dir, f"{tid}.db")
            try:
                DB(path).conn.close()                    # creates the file and schema; failure rolls the control row back
            except Exception:
                self._unlink(path)
                raise
            c.execute("UPDATE tenants SET data_path=? WHERE id=?", (path, tid))
        return Tenant(tid, name, key)

    def delete_tenant(self, tenant_id: int) -> None:
        row = self.control.one("SELECT data_path FROM tenants WHERE id=?", (tenant_id,))
        if not row:
            raise KeyError(tenant_id)
        with self.control.tx() as c:
            c.execute("UPDATE tenants SET status='deleted' WHERE id=?", (tenant_id,))       # new requests fail immediately
        self.evict(tenant_id)
        with self.control.tx() as c:
            c.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
        self._unlink(row["data_path"])
        self.control.audit("tenant_deleted", tenant_id=tenant_id)

    @staticmethod
    def _unlink(path: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass

    def tenant_by_key(self, public_key) -> Tenant | None:
        if not isinstance(public_key, str) or not public_key.startswith("pk_") or len(public_key) > 64:
            return None
        r = self.control.one("SELECT id,name,public_key FROM tenants WHERE public_key=? AND status='active'", (public_key,))
        return Tenant(r["id"], r["name"], r["public_key"]) if r else None

    # ------------------------------------------------------------------ settings
    def set_setting(self, tenant_id: int, key: str, value) -> None:
        if key not in SETTING_KEYS:
            raise ValueError(f"unknown setting {key!r}")
        typ = SETTING_KEYS[key][0]
        if isinstance(value, bool) or not isinstance(value, typ) or (typ is list and not all(isinstance(v, str) for v in value)):
            raise ValueError(f"setting {key!r} must be {typ.__name__}")
        with self.control.tx() as c:
            c.execute("INSERT INTO tenant_settings(tenant_id,key,value_enc) VALUES(?,?,?) ON CONFLICT(tenant_id,key) DO UPDATE SET value_enc=excluded.value_enc",
                      (tenant_id, key, self.enc.encrypt(json.dumps(value))))

    def public_settings(self, tenant_id: int) -> dict:
        """Settings for display: secrets are reported as set/unset, never returned."""
        out: dict = {}
        for r in self.control.q("SELECT key,value_enc FROM tenant_settings WHERE tenant_id=?", (tenant_id,)):
            if r["key"] in SETTING_KEYS:
                out[r["key"]] = True if SETTING_KEYS[r["key"]][1] else json.loads(self.enc.decrypt(r["value_enc"]))
        return out

    def settings_for(self, tenant_id: int) -> Settings:
        t = self.control.one("SELECT public_key,data_path FROM tenants WHERE id=? AND status='active'", (tenant_id,))
        if not t:
            raise KeyError(tenant_id)
        overlay = {r["key"]: json.loads(self.enc.decrypt(r["value_enc"])) for r in
                   self.control.q("SELECT key,value_enc FROM tenant_settings WHERE tenant_id=?", (tenant_id,)) if r["key"] in SETTING_KEYS}
        name = self.control.one("SELECT name FROM tenants WHERE id=?", (tenant_id,))["name"]
        # Start from a clean slate: nothing the operator's environment holds (connector credentials, webhook secrets, licence
        # identity, DNC file) may leak into a workspace. Only what the workspace itself set is applied.
        clean = dict(_BLANK, agency_name=name)
        return dataclasses.replace(self.base, **{**clean, **overlay}, api_key="", db_path=t["data_path"], control_db="",
                                   public_url=f"{self.base.base_url.rstrip('/')}/t/{t['public_key']}")

    # ------------------------------------------------------------------ runtimes
    def _runtime(self, tenant_id: int) -> _Runtime:
        with self._lock:
            rt = self._cache.get(tenant_id)
            if rt is not None:
                self._cache.move_to_end(tenant_id)
                return rt
            s = self.settings_for(tenant_id)                      # raises KeyError for unknown/deleted workspaces
            db = DB(s.db_path)
            rt = _Runtime(create_app(s, db), db)
            self._cache[tenant_id] = rt
            while len(self._cache) > self.max_open:
                old_id, old = next(iter(self._cache.items()))
                if old_id == tenant_id:
                    break
                self._drop(old_id)
            return rt

    def _drop(self, tenant_id: int) -> None:
        rt = self._cache.pop(tenant_id, None)
        if rt:
            rt.evicted = True
            if rt.refs == 0:
                self._close(rt)

    @staticmethod
    def _close(rt: _Runtime) -> None:
        try:
            rt.db.conn.close()
        except Exception:
            pass

    def evict(self, tenant_id: int) -> None:
        with self._lock:
            self._drop(tenant_id)

    def app_for(self, tenant_id: int):
        return self._runtime(tenant_id).app

    def db_for(self, tenant_id: int) -> DB:
        return self._runtime(tenant_id).db

    @contextmanager
    def lease(self, tenant_id: int) -> Iterator:
        """Hold a workspace app for the duration of a request: eviction cannot close its database underneath you."""
        with self._lock:
            rt = self._runtime(tenant_id)
            rt.refs += 1
        try:
            yield rt.app
        finally:
            with self._lock:
                rt.refs -= 1
                if rt.evicted and rt.refs == 0:
                    self._close(rt)
