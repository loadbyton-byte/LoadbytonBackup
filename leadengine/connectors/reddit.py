"""Reddit via the official OAuth API (app-only). Searches UAE property subreddits for buying/renting intent."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Iterable

from ..config import Settings
from ..models import Signal
from .base import Connector, http_json

DEFAULT_SUBS = ["dubai", "DubaiProperty", "DubaiRealEstate", "UAE", "abudhabi", "DubaiPetrolHeads", "dubaijobs", "Sharjah"]
DEFAULT_QUERIES = ['"looking to buy"', '"looking to rent"', "off plan", "mortgage", '"golden visa" property', "recommend agent",
                   "villa budget", "apartment rent"]


class RedditConnector(Connector):
    name, platform, basis = "reddit", "reddit", "official_api"
    description = "Reddit official API: public posts in UAE subreddits (respect Reddit's Data API terms & rate limits)."

    def __init__(self, settings: Settings, http=http_json):
        self.s, self.http = settings, http

    def _token(self) -> str:
        auth = base64.b64encode(f"{self.s.reddit_client_id}:{self.s.reddit_client_secret}".encode()).decode()
        r = self.http("https://www.reddit.com/api/v1/access_token", method="POST", form=True,
                      headers={"Authorization": f"Basic {auth}", "User-Agent": self.s.reddit_user_agent},
                      data={"grant_type": "client_credentials"})
        return r["access_token"]

    def fetch(self, subreddits: list[str] | None = None, queries: list[str] | None = None, limit: int = 50, **_) -> Iterable[Signal]:
        if not (self.s.reddit_client_id and self.s.reddit_client_secret):
            raise RuntimeError("Set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (create a 'script' app at reddit.com/prefs/apps).")
        token = self._token()
        hdr = {"Authorization": f"bearer {token}", "User-Agent": self.s.reddit_user_agent}
        seen: set[str] = set()
        for sub in subreddits or DEFAULT_SUBS:
            for q in queries or DEFAULT_QUERIES:
                url = (f"https://oauth.reddit.com/r/{sub}/search?restrict_sr=1&sort=new&limit={min(limit, 100)}"
                       f"&q={q.replace(' ', '%20').replace(chr(34), '%22')}")
                for child in self.http(url, headers=hdr).get("data", {}).get("children", []):
                    d = child["data"]
                    if d["id"] in seen:
                        continue
                    seen.add(d["id"])
                    yield Signal(
                        platform="reddit", external_id=d["id"], source_type="public_post",
                        text=f"{d.get('title', '')}\n{d.get('selftext', '')}".strip(),
                        url="https://www.reddit.com" + d.get("permalink", ""), author_handle=d.get("author"),
                        created_at=datetime.fromtimestamp(d["created_utc"], tz=timezone.utc),
                        extra={"subreddit": d.get("subreddit")})
