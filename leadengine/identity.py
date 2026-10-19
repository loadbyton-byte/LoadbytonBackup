"""Identifier normalisation + cross-platform identity resolution."""
from __future__ import annotations

import hashlib
import re

_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str | None) -> str | None:
    """Normalise UAE-style numbers to E.164. Returns None if it doesn't look like a valid number."""
    if not raw:
        return None
    s = raw.strip()
    plus = s.startswith("+")
    d = _DIGITS.sub("", s)
    if d.startswith("00"):
        d, plus = d[2:], True
    if plus or d.startswith("971"):
        if d.startswith("971") and 11 <= len(d) <= 12:
            return "+" + d
        if plus and 8 <= len(d) <= 15:        # foreign number: keep as-is
            return "+" + d
        return None
    if d.startswith("0") and len(d) in (9, 10):  # 05x xxx xxxx / 04 xxx xxxx
        return "+971" + d[1:]
    if len(d) == 9 and d.startswith("5"):         # 5x xxx xxxx
        return "+971" + d
    return None


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else None


def normalize_handle(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.strip().lstrip("@").lower().removeprefix("u/") or None


def hash_identifier(kind: str, value: str) -> str:
    """One-way hash used by the suppression list so we can honour erasure without keeping the data."""
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def identifiers_for(platform: str, handle: str | None, phone: str | None, email: str | None) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []
    p, e, h = normalize_phone(phone), normalize_email(email), normalize_handle(handle)
    if p:
        ids.append(("phone", p))
    if e:
        ids.append(("email", e))
    if h:
        ids.append((f"handle:{platform}", h))
    return ids
