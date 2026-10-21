"""Server-side state for the web concierge. The raw token lives only in the visitor's page; the database holds its SHA-256, so a database
leak cannot resume anyone's chat. A session is NOT a lead: it holds the visitor's answers until they consent, then it is deleted."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from ..db import DB

TTL = timedelta(hours=24)
MAX_PER_IP = 3
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WebSessions:
    def __init__(self, db: DB):
        self.db = db

    def start(self, lang: str, ip: str, now: datetime | None = None) -> str:
        now = now or _now()
        ip_hash = _h("ip:" + (ip or "?"))
        live = self.db.one("SELECT COUNT(*) n FROM web_sessions WHERE ip_hash=? AND expires_at>?", (ip_hash, now.isoformat()))["n"]
        if live >= MAX_PER_IP:
            raise ValueError("too many sessions")
        raw = secrets.token_urlsafe(32)
        state = {"step": "intent", "answers": {}, "retries": {}, "lang": lang, "phase": "qa"}
        with self.db.tx() as c:
            c.execute("INSERT INTO web_sessions(token_hash,state,lang,ip_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                      (_h(raw), json.dumps(state), lang, ip_hash, now.isoformat(), (now + TTL).isoformat()))
        return raw

    def load(self, token, now: datetime | None = None) -> dict | None:
        if not isinstance(token, str) or not _TOKEN.match(token):
            return None
        row = self.db.one("SELECT state, expires_at FROM web_sessions WHERE token_hash=?", (_h(token),))
        if not row or row["expires_at"] <= (now or _now()).isoformat():
            return None
        return json.loads(row["state"])

    def save(self, token: str, state: dict) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE web_sessions SET state=? WHERE token_hash=?", (json.dumps(state), _h(token)))

    def delete(self, token: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM web_sessions WHERE token_hash=?", (_h(token),))

    def purge(self, now: datetime | None = None) -> int:
        with self.db.tx() as c:
            return c.execute("DELETE FROM web_sessions WHERE expires_at<=?", ((now or _now()).isoformat(),)).rowcount
