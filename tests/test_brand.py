from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB

D = Path(__file__).resolve().parent.parent / "dashboard"
ASSETS = ["brand/wordmark-clear.png", "brand/wordmark.webp", "brand/favicon.svg", "brand/favicon.png", "brand/favicon-192.png",
          "brand/favicon-512.png", "brand/apple-touch-icon.png", "brand/opengraph.png", "brand/real-estate.webp", "brand/NOTICE.md",
          "fonts/plus-jakarta-sans-latin-wght-normal.woff2", "fonts/instrument-sans-latin-wght-normal.woff2", "theme.css"]


@pytest.fixture()
def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))


@pytest.mark.parametrize("rel", ASSETS)
def test_brand_assets_exist(rel):
    assert (D / rel).is_file() and (D / rel).stat().st_size > 0


def test_static_served(client):
    r = client.get("/static/brand/wordmark-clear.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/static/theme.css").status_code == 200


def test_theme_has_brand_tokens():
    css = (D / "theme.css").read_text()
    for token in ("#f23a1d", "#0f1714", "#f6f5f1", "#c9260e", "#ff7a45", "#0a211b", "#3ecf6e", "Plus Jakarta Sans",
                  "Instrument Sans", "prefers-color-scheme: dark"):
        assert token in css, token


def test_dashboard_uses_brand(client):
    html = client.get("/").text
    assert "/static/theme.css" in html and "wordmark-clear.png" in html and "<title>DigitalBurj Leads" in html
