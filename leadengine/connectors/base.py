from __future__ import annotations

import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Iterable

from ..compliance import assert_connector_allowed
from ..models import Signal


def http_json(url: str, *, method: str = "GET", headers: dict | None = None, data: dict | bytes | None = None,
              form: bool = False, timeout: int = 30) -> dict | list:
    """Tiny stdlib HTTP helper (kept dependency-free; swap for httpx/requests if you prefer)."""
    body = None
    h = dict(headers or {})
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    elif data:
        body = data
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URLs only, supplied by config
        return json.loads(r.read().decode())


class Connector(ABC):
    name: str = ""
    platform: str = ""
    basis: str = ""          # must be a compliance.Basis value — enforced at registration
    description: str = ""

    @abstractmethod
    def fetch(self, **params) -> Iterable[Signal]:
        """Yield normalised Signals. Must use only the lawful basis declared above."""


class ConnectorRegistry:
    def __init__(self) -> None:
        self._c: dict[str, Connector] = {}

    def register(self, c: Connector) -> None:
        assert_connector_allowed(c.name, c.basis)
        self._c[c.name] = c

    def get(self, name: str) -> Connector:
        if name not in self._c:
            raise KeyError(f"unknown connector '{name}'. Registered: {sorted(self._c)}")
        return self._c[name]

    def describe(self) -> list[dict]:
        return [{"name": c.name, "platform": c.platform, "basis": c.basis, "description": c.description} for c in self._c.values()]
