"""Headless-browser checks of the Deals page. Skipped without Playwright/Chromium."""
import pytest

pytest.importorskip("playwright.sync_api")

from fastapi.testclient import TestClient  # noqa: E402

from .test_dashboard_browser import XSS, link_token, sign_in, workspace  # noqa: E402,F401


def seeded(live):
    email, c, h = workspace(live, seed=False)
    key = c.get("/auth/me").json()["workspace"]["public_key"]
    TestClient(live.app).post(f"/t/{key}/capture", json={"name": XSS, "phone": "0501234567", "email": "s@example.com", "consent": True})
    return email, c, h


def test_add_move_and_lose_a_deal(live, browser_page):
    email, c, h = seeded(live)
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/deals']")
    pg.wait_for_selector("#dl-lead option", state="attached")
    assert pg.evaluate("window.__xss") is None
    pg.fill("#dl-value", "2000000")
    pg.fill("#dl-title", "Marina 2BR")
    pg.click("#dl-form button[type=submit]")
    pg.wait_for_selector(".dl-col[data-stage=viewing_done] .dl-card")
    assert pg.locator("[data-kpi=pipeline]").inner_text() == "AED 2M" and pg.locator("[data-kpi=expected]").inner_text() == "AED 40K"
    pg.click(".dl-col[data-stage=viewing_done] .dl-move")
    pg.wait_for_selector(".dl-col[data-stage=offer_made] .dl-card")
    pg.click(".dl-col[data-stage=offer_made] .dl-lose")
    pg.click(".dl-confirm-lose")                                                          # empty reason: nothing happens
    assert pg.locator(".dl-col[data-stage=lost] .dl-card").count() == 0
    pg.fill(".dl-reason", "bought elsewhere")
    pg.click(".dl-confirm-lose")
    pg.wait_for_selector(".dl-col[data-stage=lost] .dl-card")
    assert "bought elsewhere" in pg.inner_text(".dl-col[data-stage=lost]") and pg.problems == []
    assert c.get("/leads").json()[0]["status"] == "lost"


def test_rtl_phone_width_and_agent_sees_page(live, browser_page):
    email, c, h = seeded(live)
    pg = sign_in(live, browser_page, email, width=375, height=800)
    pg.goto(live.url + "/app#/deals")
    pg.wait_for_selector("#dl-board")
    pg.click("#lang-btn")
    pg.wait_for_selector("#view h1:has-text('الصفقات')")
    assert pg.evaluate("document.documentElement.dir") == "rtl"
    assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    assert pg.problems == []
