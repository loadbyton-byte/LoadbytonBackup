"""Role policy for the workspace API. First matching rule wins; anything unmatched is OWNER-ONLY (default deny for new routes).

Paths are matched exactly as written (case-sensitive) after `normalize_path`, which rejects anything that could be a traversal or
encoding trick: the dispatcher must forward the normalised path it checked, never a different spelling of it."""
from __future__ import annotations

import re

ROLE_RANK = {"agent": 1, "manager": 2, "owner": 3}
MAX_PATH = 512
_ID = r"\d{1,12}"


def normalize_path(path) -> str | None:
    """Return the path with at most one trailing slash removed, or None if it looks like a traversal/encoding trick."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH or not path.isascii():
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path) or "\\" in path or "%" in path or "//" in path:
        return None
    if any(seg in (".", "..") for seg in path.split("/")):
        return None
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


# (methods, path regex, minimum role). HEAD is treated as GET.
_RULES: list[tuple[frozenset[str], re.Pattern[str], str]] = [
    # owner-only operations (checked before the broader manager rules)
    (frozenset({"DELETE"}), re.compile(r"^/leads/" + _ID + "$"), "owner"),
    (frozenset({"POST"}), re.compile(r"^/maintenance/purge$"), "owner"),
    (frozenset({"POST"}), re.compile(r"^/demo/seed$"), "owner"),
    (frozenset({"POST"}), re.compile(r"^/markets/backfill-origin$"), "owner"),
    # agent-level: only the leads they work (row scoping is enforced by the dispatcher) and their day
    (frozenset({"GET"}), re.compile(r"^/leads$"), "agent"),
    (frozenset({"GET"}), re.compile(r"^/leads/" + _ID + r"(/(matches|next-actions|deal-sheet))?$"), "agent"),
    (frozenset({"POST"}), re.compile(r"^/leads/" + _ID + r"/(status|activities|viewings|outreach)$"), "agent"),
    (frozenset({"GET"}), re.compile(r"^/tasks/today$"), "agent"),
    (frozenset({"GET"}), re.compile(r"^/viewings$"), "agent"),
    # manager-level: everything else the workspace API offers
    (frozenset({"GET", "POST", "PATCH"}),
     re.compile(r"^/(agents|audit|connectors|ingest|inventory|leads|magnets|markets|overview|radar|roi|spend|stats|tasks|tools|viewings(\.ics)?|maintenance/(escalate|rescore))(/.*)?$"),
     "manager"),
]


def allowed(role, method: str, path: str) -> bool:
    rank = ROLE_RANK.get(role)
    p = normalize_path(path)
    method = (method or "").upper()
    if rank is None or p is None or method in ("OPTIONS", "TRACE", "CONNECT"):
        return False
    method = "GET" if method == "HEAD" else method
    for methods, pattern, minimum in _RULES:
        if method in methods and pattern.match(p):
            return rank >= ROLE_RANK[minimum]
    return rank >= ROLE_RANK["owner"]
