"""ASGI factory for `uvicorn leadengine.main:create --factory`: platform mode when CONTROL_DB is set, otherwise the single-tenant app."""
from __future__ import annotations

from .config import Settings


def create():
    s = Settings()
    if s.control_db:
        from .platform.app import create_platform_app
        return create_platform_app(s)
    from .api import create_app
    return create_app(s)
