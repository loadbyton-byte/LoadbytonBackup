"""Control database: users, workspaces, sessions, tokens, settings and audit. Workspace *data* lives in per-workspace files."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL DEFAULT '', name TEXT,
  lang TEXT DEFAULT 'en', email_verified_at TEXT, totp_secret_enc TEXT, totp_enabled INTEGER DEFAULT 0, totp_last_step INTEGER DEFAULT 0,
  failed_logins INTEGER DEFAULT 0, locked_until REAL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, public_key TEXT NOT NULL UNIQUE, data_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', api_key_hash TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('owner','manager','agent')), agent_id INTEGER, PRIMARY KEY(user_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, mfa_ok INTEGER DEFAULT 1, csrf_hash TEXT,
  created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invites(
  token_hash TEXT PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, email TEXT NOT NULL,
  role TEXT NOT NULL, agent_id INTEGER, expires_at REAL NOT NULL, accepted_at REAL
);
CREATE TABLE IF NOT EXISTS auth_tokens(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL,
  expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS recovery_codes(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, code_hash TEXT NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_attempts ON login_attempts(key, ts);
CREATE TABLE IF NOT EXISTS tenant_settings(
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, key TEXT NOT NULL, value_enc TEXT NOT NULL, PRIMARY KEY(tenant_id, key)
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id INTEGER, tenant_id INTEGER, action TEXT NOT NULL, detail TEXT, ip TEXT
);
"""
_SECRET_HINTS = ("password", "token", "secret", "code", "key")


class ControlDB:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:" and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def audit(self, action: str, *, user_id: int | None = None, tenant_id: int | None = None, ip: str | None = None, **detail: Any) -> None:
        """Append an audit record. Refuses detail keys that look like secrets so credentials can never be logged by accident."""
        for k in detail:
            if any(h in k.lower() for h in _SECRET_HINTS):
                raise ValueError(f"audit detail key {k!r} looks like a secret; log an event name instead")
        with self.tx() as c:
            c.execute("INSERT INTO audit(ts,user_id,tenant_id,action,detail,ip) VALUES(?,?,?,?,?,?)",
                      (datetime.now(timezone.utc).isoformat(), user_id, tenant_id, action, json.dumps(detail, default=str), ip))
