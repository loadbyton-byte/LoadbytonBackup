"""SQLite persistence. Swap for Postgres by re-implementing this thin module."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT, phone TEXT, email TEXT,
  label TEXT, stage TEXT, score INTEGER DEFAULT 0, tier TEXT DEFAULT 'DISCARD',
  primary_area TEXT, emirate TEXT, budget_max REAL, bedrooms INTEGER,
  purchase_type TEXT, timeline_days INTEGER, financing TEXT, language TEXT,
  consent_status TEXT DEFAULT 'public_signal', consent_evidence TEXT,
  opted_out INTEGER DEFAULT 0, status TEXT DEFAULT 'new', assigned_to TEXT,
  sla_due_at TEXT, first_seen TEXT, last_seen TEXT, platforms TEXT DEFAULT '[]',
  breakdown TEXT DEFAULT '{}', profile TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_leads_score ON leads(score DESC);
CREATE TABLE IF NOT EXISTS identifiers(
  kind TEXT NOT NULL, value TEXT NOT NULL, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  PRIMARY KEY(kind, value)
);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  platform TEXT, external_id TEXT, source_type TEXT, text TEXT, url TEXT, author_handle TEXT,
  created_at TEXT, ingested_at TEXT, label TEXT, stage TEXT, score INTEGER, areas TEXT, budget_max REAL,
  campaign TEXT, intent TEXT,
  UNIQUE(platform, external_id)
);
CREATE INDEX IF NOT EXISTS ix_signals_created ON signals(created_at);
CREATE TABLE IF NOT EXISTS suppression(hash TEXT PRIMARY KEY, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, lead_id INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS activities(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
  ts TEXT, kind TEXT, channel TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions(
  token_hash TEXT PRIMARY KEY, state TEXT NOT NULL, lang TEXT, ip_hash TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_web_sessions_expires ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sequences(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, trigger TEXT NOT NULL, enabled INTEGER DEFAULT 1,
  steps TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sequence_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE, state TEXT DEFAULT 'active',
  started_at TEXT, stopped_at TEXT, stop_reason TEXT, UNIQUE(lead_id, sequence_id)
);
CREATE TABLE IF NOT EXISTS touches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES sequence_runs(id) ON DELETE CASCADE,
  lead_id INTEGER NOT NULL, step_index INTEGER NOT NULL, channel TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT DEFAULT 'pending',
  body TEXT, note TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, sent_at TEXT, approved_by TEXT, UNIQUE(run_id, step_index)
);
CREATE INDEX IF NOT EXISTS ix_touches_due ON touches(status, due_at);
CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS spend(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, campaign TEXT, period TEXT NOT NULL, amount_aed REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, developer TEXT, area TEXT, emirate TEXT, min_price REAL, max_price REAL,
  bedrooms TEXT DEFAULT '[]', handover_year INTEGER, payment_plan TEXT
);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE, channel TEXT NOT NULL,
  state TEXT DEFAULT '{}', transcript TEXT DEFAULT '[]', updated_at TEXT, UNIQUE(lead_id, channel)
);
CREATE TABLE IF NOT EXISTS viewings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE, agent_id INTEGER,
  start_ts TEXT NOT NULL, duration_min INTEGER NOT NULL, location TEXT DEFAULT '', status TEXT DEFAULT 'scheduled', created_at TEXT
);
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT, email TEXT,
  areas TEXT DEFAULT '[]', languages TEXT DEFAULT '["en"]', weight INTEGER DEFAULT 1, max_open INTEGER DEFAULT 25, active INTEGER DEFAULT 1
);

"""


# Columns added after the first release: (table, column, declaration). Applied idempotently to existing databases.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("leads", "assigned_agent_id", "INTEGER"),
    ("leads", "escalation_level", "INTEGER DEFAULT 0"),
    ("signals", "listing_ref", "TEXT"),
    ("leads", "origin_country", "TEXT"),
    ("leads", "origin_city", "TEXT"),
    ("leads", "origin_basis", "TEXT"),
    ("signals", "origin_country", "TEXT"),
    ("signals", "origin_city", "TEXT"),
    ("signals", "origin_basis", "TEXT"),
    ("spend", "country", "TEXT"),
    ("spend", "city", "TEXT"),
]


class DB:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, decl in MIGRATIONS:
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
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

    def bump(self, key: str, n: int = 1) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?", (key, n, n))

    def audit(self, actor: str, action: str, lead_id: int | None = None, **detail: Any) -> None:
        from .models import utcnow
        with self.tx() as c:
            c.execute("INSERT INTO audit_log(ts,actor,action,lead_id,detail) VALUES(?,?,?,?,?)",
                      (utcnow().isoformat(), actor, action, lead_id, json.dumps(detail, default=str)))
