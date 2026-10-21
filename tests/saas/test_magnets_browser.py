"""Headless-browser checks of the public lead-magnet pages and the dashboard card. Skipped without Playwright/Chromium."""
import pytest

pytest.importorskip("playwright.sync_api")

from fastapi.testclient import TestClient  # noqa: E402

from .test_dashboard_browser import sign_in, workspace  # noqa: E402

AFF = {"income": "30000", "debts": "5000", "down": "200000", "years": "25", "rate": "4.5"}


def key_of(c):
    return c.get("/auth/me").json()["workspace"]["public_key"]


def test_settings_has_public_pages_card_with_urls_and_counters(live, browser_page):
    email, c, h = workspace(live, seed=False)
    key = key_of(c)
    TestClient(live.app).get(f"/t/{key}/p/affordability")
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/settings']")
    pg.wait_for_selector("#public-pages [data-pub]")
    assert pg.locator("#public-pages [data-pub]").count() == 5
    link = pg.locator("[data-pub=costs] a")
    assert link.get_attribute("href").endswith(f"/t/{key}/p/costs") and link.get_attribute("rel") == "noopener"
    assert "<iframe" in pg.inner_text("[data-pub=costs] pre") and "Views: 0" in pg.inner_text("[data-pub=costs]")
    assert "Views: 1" in pg.inner_text("[data-pub=affordability]")
    assert pg.problems == []


def test_public_calculator_flow_without_javascript(live, browser_page):
    email, c, h = workspace(live, seed=False)
    key = key_of(c)
    pg = browser_page()
    ctx = pg.context.browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    errors = []
    page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.goto(f"{live.url}/t/{key}/p/affordability")
    for name, value in AFF.items():
        page.fill(f"#f-{name}", value)
    page.press("#f-rate", "Enter")
    page.wait_for_selector("dl.kv")
    assert "1,999,103.22" in page.inner_text("main") and "Indicative only" in page.inner_text("main")
    page.fill("#l-name", "Browser Buyer")
    page.fill("#l-phone", "0501234567")
    page.check("input[name=consent]")
    page.click("form[action$='/p/lead'] button[type=submit]")
    page.wait_for_selector("text=Thank you")
    assert len(c.get("/leads").json()) == 1 and errors == []
    ctx.close()


def test_consent_box_blocks_submission_in_the_browser(live, browser_page):
    email, c, h = workspace(live, seed=False)
    key = key_of(c)
    pg = browser_page()
    pg.goto(f"{live.url}/t/{key}/p/costs")
    pg.fill("#f-price", "2000000")
    pg.press("#f-price", "Enter")
    pg.wait_for_selector("dl.kv")
    pg.fill("#l-phone", "0501234567")
    pg.click("form[action$='/p/lead'] button[type=submit]")                           # the required checkbox blocks the native submit
    assert pg.url.endswith("/p/costs") and c.get("/leads").json() == []


def test_concierge_in_arabic_rtl_at_phone_width_and_keyboard_only(live, browser_page):
    email, c, h = workspace(live, seed=False)
    key = key_of(c)
    pg = browser_page(width=375, height=800, locale="ar-AE")
    pg.goto(f"{live.url}/t/{key}/p/concierge?lang=ar")
    assert pg.evaluate("document.documentElement.dir") == "rtl"
    for answer in ("اشتري", "دبي مارينا", "2 مليون", "هذا الشهر", "نعم"):
        pg.wait_for_selector("#c-answer")
        pg.fill("#c-answer", answer)
        pg.press("#c-answer", "Enter")
        assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    pg.wait_for_selector("#c-phone")
    pg.fill("#c-name", "سارة")
    pg.fill("#c-phone", "0501234567")
    pg.press("#c-phone", "Enter")
    pg.wait_for_selector("text=شكراً")
    got = c.get("/leads").json()
    assert len(got) == 1 and got[0]["primary_area"] == "Dubai Marina"
    assert pg.problems == []


def test_framing_is_limited_to_listed_origins(live, browser_page):
    email, c, h = workspace(live, seed=False)
    key = key_of(c)
    c.put("/auth/settings", json={"capture_origins": ["http://localhost.test"]}, headers=h)
    r = TestClient(live.app).get(f"/t/{key}/p")
    assert "frame-ancestors 'self' http://localhost.test" in r.headers["content-security-policy"] and "x-frame-options" not in r.headers
    r2 = TestClient(live.app).get(f"/t/{key}/p", headers={"Origin": "http://evil.test"})
    assert "evil.test" not in r2.headers["content-security-policy"]
