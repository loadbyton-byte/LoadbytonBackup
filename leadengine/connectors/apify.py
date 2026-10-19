"""Licensed-provider connector via Apify (or any vendor returning JSON items).

You choose the actor/dataset and are responsible for its licence and the target platform's terms — this
connector only maps returned items into Signals. It declares basis=licensed_provider; do not point it at
actors that log in with borrowed credentials or bypass access controls."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..config import Settings
from ..models import Signal
from .base import Connector, http_json


def _dig(item: dict, path: str | None):
    if not path:
        return None
    cur = item
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class ApifyConnector(Connector):
    name, basis = "apify", "licensed_provider"
    platform = "multi"
    description = "Run an Apify actor you are licensed to use; map its items to signals (LinkedIn/FB/IG/TikTok public data)."

    DEFAULT_MAP = {"external_id": "id", "text": "text", "url": "url", "author_handle": "author.username",
                   "author_name": "author.name", "created_at": "createdAt"}

    def __init__(self, settings: Settings, http=http_json):
        self.s, self.http = settings, http

    def fetch(self, actor_id: str = "", platform: str = "", run_input: dict | None = None, mapping: dict | None = None,
              source_type: str = "public_post", **_) -> Iterable[Signal]:
        if not self.s.apify_token:
            raise RuntimeError("Set APIFY_TOKEN.")
        if not (actor_id and platform):
            raise ValueError("actor_id and platform are required")
        url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token={self.s.apify_token}"
        items = self.http(url, method="POST", data=run_input or {}, timeout=300)
        m = {**self.DEFAULT_MAP, **(mapping or {})}
        for it in items:
            ext = _dig(it, m["external_id"]) or _dig(it, m["url"])
            text = _dig(it, m["text"])
            if not (ext and text):
                continue
            ts = _dig(it, m["created_at"])
            try:
                created = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
            except ValueError:
                created = datetime.now(timezone.utc)
            yield Signal(platform=platform, external_id=str(ext), source_type=source_type, text=str(text),
                         url=_dig(it, m["url"]), author_handle=_dig(it, m["author_handle"]),
                         author_name=_dig(it, m["author_name"]), created_at=created)
