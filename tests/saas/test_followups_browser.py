"""Headless-browser checks of the Follow-up page. Skipped without Playwright/Chromium."""
import pytest

pytest.importorskip("playwright.sync_api")

from fastapi.testclient import TestClient  # noqa: E402

from .test_dashboard_browser import XSS, link_token, sign_in, workspace  # noqa: E402,F401

FORM = {"name": XSS, "phone": "0501234567", "email": "sara@example.com", "consent": True, "area": "Dubai Marina", "looking_to": "buy"}


def seeded(live):
    email, c, h = workspace(live, seed=False)
    key = c.get("/auth/me").json()["workspace"]["public_key"]
    assert TestClient(live.app).post(f"/t/{key}/capture", json=FORM).status_code == 200
    assert c.post("/maintenance/followups", headers=h).json()["queued"] == 1
    return email, c, h


def test_queue_edit_and_approve(live, browser_page):
    email, c, h = seeded(live)
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/followups']")
    pg.wait_for_selector(".fu-item")
    assert pg.locator(".fu-item").count() == 1 and pg.locator("[data-kpi=queued]").inner_text() == "1"
    assert pg.evaluate("window.__xss") is None                                            # hostile lead name is shown as text, never run
    pg.fill(".fu-body", "Hello, edited by a person")
    pg.click(".fu-approve")
    pg.wait_for_selector("#fu-empty:not([hidden])")
    assert pg.locator("[data-kpi=queued]").inner_text() == "0"
    assert pg.problems == []


def test_autosend_toggle_and_sequence_save(live, browser_page):
    email, c, h = workspace(live, seed=False)
    pg = sign_in(live, browser_page, email)
    pg.goto(live.url + "/app#/followups")
    pg.wait_for_selector("#fu-seq .fu-step")
    assert not pg.is_checked("#fu-auto")
    pg.check("#fu-auto")
    pg.wait_for_selector("#fu-msg:has-text('on')")
    assert c.get("/followups/settings").json()["autosend"] is True
    pg.fill(".fu-sequence:first-of-type .fu-en", "Hi {name}, edited in the browser")
    pg.click("#fu-save")
    pg.wait_for_selector("#fu-save-msg:has-text('Saved')")
    assert c.get("/followups/sequences").json()[0]["steps"][0]["template_en"] == "Hi {name}, edited in the browser"
    pg.fill(".fu-sequence:first-of-type .fu-delay", "99999")
    pg.click("#fu-save")
    pg.wait_for_selector("#fu-save-msg .no")
    assert pg.problems == [] or all("422" in p for p in pg.problems)


def test_rtl_phone_width_and_agent_has_no_page(live, browser_page):
    email, c, h = seeded(live)
    pg = sign_in(live, browser_page, email, width=375, height=800)
    pg.goto(live.url + "/app#/followups")
    pg.wait_for_selector(".fu-item")
    pg.click("#lang-btn")
    pg.wait_for_selector("#view h1:has-text('المتابعة')")
    assert pg.evaluate("document.documentElement.dir") == "rtl"
    assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    c.post("/auth/invite", json={"email": "agentf@agency.ae", "role": "agent"}, headers=h)
    a = browser_page()
    a.goto(live.url + "/invite?token=" + link_token(live.mailer, "invite"))
    a.fill("input[name=name]", "Ag")
    a.fill("input[name=password]", "correct horse battery")
    a.click("button[type=submit]")
    a.wait_for_url("**/login")
    a.fill("input[name=email]", "agentf@agency.ae")
    a.fill("input[name=password]", "correct horse battery")
    a.press("input[name=password]", "Enter")
    a.wait_for_url("**/app")
    a.wait_for_selector("#view h1")
    assert not a.is_visible(".side nav a[href='#/followups']")
    a.goto(live.url + "/app#/followups")
    a.wait_for_selector("#view h1")
    assert "Follow-up" not in a.inner_text("#view h1")
