"""Workspace management. (Task 4 defines only the Tenant record; the manager arrives in Task 6.)"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    id: int
    name: str
    public_key: str
