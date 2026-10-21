"""Headless-browser checks of the dashboard shell. Skipped without Playwright/Chromium."""
import re
import time

import pytest

pytest.importorskip("playwright.sync_api")

from fastapi.testclient import TestClient  # noqa: E402

from leadengine.platform.security import totp_code  # noqa: E402

from .conftest import PW  # noqa: E402
from .test_app_auth import SIG, link_token, login  # noqa: E402

XSS = '<img src=x onerror="window.__xss=1">'
CSV = "name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\nMarina Edge,Acme,Dubai Marina,Dubai,1.2M,2.4M,2,2030,60/40\n"
_n = {"i": 0}


def workspace(live, seed=True):
    """Register + verify a fresh workspace (in-process, same app as the live server) and optionally seed it."""
    _n["i"] += 1
    email = f"dash{_n['i']}@agency.ae"
    c = TestClient(live.app)
    assert c.post("/auth/register", json={"email": email, "password": PW, "name": "Dana", "agency_name": f"Dash Realty {_n['i']}"}).status_code == 200
    assert c.post("/auth/verify", json={"token": link_token(live.mailer, "verify")}).status_code == 200
    h = login(c, email)
    if seed:
        sigs = [{**SIG[0], "external_id": f"d{_n['i']}{k}", "author_handle": f"person_{_n['i']}_{k}", "author_name": XSS if k == 0 else f"Lead {k}"} for k in range(3)]
        assert c.post("/ingest/signals", json=sigs, headers=h).status_code == 200
        c.post("/agents", json={"name": "Layla", "languages": ["en", "ar"], "areas": ["Dubai Marina"]}, headers=h)
        c.post("/inventory", content=CSV, headers={**h, "content-type": "text/csv"})
        c.post("/spend", json={"source": "reddit", "period": "2026-10", "amount_aed": 500}, headers=h)
    return email, c, h


def sign_in(live, make, email, **kw):
    pg = make(**kw)
    pg.goto(live.url + "/login")
    pg.fill("input[name=email]", email)
    pg.fill("input[name=password]", PW)
    pg.press("input[name=password]", "Enter")
    pg.wait_for_url("**/app")
    pg.wait_for_selector("#view h1")
    return pg


def test_shell_overview_kpis_and_no_problems(live, browser_page):
    email, _, _ = workspace(live)
    pg = sign_in(live, browser_page, email)
    assert pg.is_visible(".side nav a[href='#/overview']") and pg.is_visible("#ws") and "Dash Realty" in pg.inner_text("#ws")
    assert pg.inner_text("#view h1") == "Overview"
    assert pg.inner_text("[data-kpi=ov_new_today]") == "3"
    assert int(pg.inner_text("[data-kpi=ov_hot]")) + int(pg.inner_text("[data-kpi=ov_warm]")) >= 1
    assert pg.locator("#view svg rect").count() == 14
    assert pg.evaluate("window.__xss") is None
    assert pg.problems == []


def test_command_palette_filters_and_navigates(live, browser_page):
    email, _, _ = workspace(live)
    pg = sign_in(live, browser_page, email)
    pg.keyboard.press("Control+k")
    assert pg.is_visible("#palette #pq")
    assert pg.locator("#plist button").count() >= 8
    pg.fill("#pq", "inventory")
    assert pg.locator("#plist button").count() == 1
    pg.press("#pq", "Enter")
    pg.wait_for_selector("#view h1:has-text('Inventory')")
    assert pg.url.endswith("#/inventory") and not pg.is_visible("#palette")
    pg.keyboard.press("Control+k")
    pg.fill("#pq", "zzzz-nothing")
    assert "Nothing matches" in pg.inner_text("#plist")
    pg.keyboard.press("Escape")
    assert not pg.is_visible("#palette")


def test_language_toggle_switches_to_rtl_and_theme_toggle_works(live, browser_page):
    email, _, _ = workspace(live)
    pg = sign_in(live, browser_page, email)
    pg.click("#lang-btn")
    pg.wait_for_selector("#view h1:has-text('نظرة عامة')")
    assert pg.evaluate("document.documentElement.dir") == "rtl" and pg.evaluate("document.documentElement.lang") == "ar"
    assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    pg.click("#lang-btn")
    pg.wait_for_selector("#view h1:has-text('Overview')")
    before = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    pg.click("#theme-btn")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") != before
    assert pg.problems == []


def test_sidebar_becomes_a_drawer_on_narrow_screens(live, browser_page):
    email, _, _ = workspace(live)
    pg = sign_in(live, browser_page, email, width=375, height=800)
    assert pg.is_visible("#menu-btn")
    assert pg.evaluate("document.querySelector('.side').getBoundingClientRect().right <= 1")      # off-screen
    pg.click("#menu-btn")
    for _ in range(40):                                                                          # after the slide-in transition
        if pg.evaluate("document.querySelector('.side').getBoundingClientRect().left >= 0"):
            break
        pg.wait_for_timeout(50)
    assert pg.evaluate("document.querySelector('.side').getBoundingClientRect().left >= 0")
    pg.click(".side nav a[href='#/leads']")
    pg.wait_for_selector("#view h1:has-text('Leads')")
    assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def test_leads_tasks_agents_inventory_roi_views_work_inside_the_shell(live, browser_page):
    email, _, _ = workspace(live)
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/leads']")
    pg.wait_for_selector("#rows tr.row")
    assert pg.locator("#rows tr.row").count() == 3 and pg.evaluate("window.__xss") is None
    assert XSS in pg.inner_text("#rows")                                                  # rendered as text, not as an element
    pg.click("#rows tr.row >> nth=1")
    pg.wait_for_selector("#dlg[open] h2")
    assert pg.is_visible("#nba")
    pg.click("#dlg button[data-act=dealsheet]")
    pg.wait_for_selector("#dealsheet dl")
    pg.click("#close")
    pg.click(".side nav a[href='#/agents']")
    pg.wait_for_selector("#agent-rows tr")
    assert "Layla" in pg.inner_text("#agent-rows")
    pg.click(".side nav a[href='#/tasks']")
    pg.wait_for_selector("#task-agent")
    pg.click(".side nav a[href='#/inventory']")
    pg.wait_for_selector("#inv-rows tr")
    assert "Marina Edge" in pg.inner_text("#inv-rows")
    pg.click(".side nav a[href='#/roi']")
    pg.wait_for_selector("#roi-rows tr")
    assert "reddit" in pg.inner_text("#roi-rows")
    assert pg.evaluate("window.__xss") is None and pg.problems == []


def test_onboarding_checklist_ticks_after_adding_an_agent(live, browser_page):
    email, _, _ = workspace(live, seed=False)
    pg = sign_in(live, browser_page, email)
    assert pg.locator(".check.done").count() == 0 and "No leads yet" in pg.inner_text("#view")
    pg.click(".side nav a[href='#/agents']")
    pg.fill("#agent-form input[name=name]", "Omar")
    pg.click("#agent-form button[type=submit]")
    pg.wait_for_selector("#agent-rows tr")
    pg.click(".side nav a[href='#/overview']")
    pg.wait_for_selector(".check")
    assert pg.locator(".check.done").count() == 1 and "Add your agents" in pg.inner_text(".check.done")
    assert pg.problems == []


def test_settings_profile_embed_apikey_mfa_and_invite(live, browser_page):
    email, c, _ = workspace(live, seed=False)
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/settings']")
    pg.wait_for_selector("#profile")
    pg.fill("#profile input[name=agent_name]", "Dana Agent")
    pg.fill("#profile input[name=brn]", "98765")
    pg.click("#profile button[type=submit]")
    pg.wait_for_selector("#profile-msg.good")
    key = c.get("/auth/me").json()["workspace"]["public_key"]
    assert key in pg.inner_text("#snippet") and f"/t/{key}/widget.js" in pg.inner_text("#snippet")
    pg.fill("#origins textarea", "https://www.agency.ae")
    pg.click("#origins button[type=submit]")
    pg.wait_for_selector("#origins-msg.good")
    # API key: shown once
    pg.click("#rotate")
    pg.wait_for_selector("#apikey")
    api_key = pg.inner_text("#apikey")
    assert api_key.startswith("dbl_") and TestClient(live.app).get("/leads", headers={"X-API-Key": api_key}).status_code == 200
    # MFA with a TOTP computed here
    pg.click("#mfa-begin")
    pg.wait_for_selector("#mfa-secret")
    secret = pg.inner_text("#mfa-secret")
    pg.fill("#mfa-confirm input[name=code]", totp_code(secret))
    pg.click("#mfa-confirm button[type=submit]")
    pg.wait_for_selector("#recovery")
    assert len(pg.inner_text("#recovery").split()) == 10
    # invite a member
    pg.fill("#invite input[name=email]", "newagent@agency.ae")
    pg.click("#invite button[type=submit]")
    pg.wait_for_selector("#invite-msg.good")
    assert link_token(live.mailer, "invite")
    # reload: settings persisted, secrets write-only
    pg.reload()
    pg.wait_for_selector("#profile")
    assert pg.input_value("#profile input[name=brn]") == "98765"
    assert pg.problems == []


def test_signed_out_users_are_sent_to_login_and_logout_works(live, browser_page):
    email, _, _ = workspace(live, seed=False)
    anon = browser_page()
    anon.goto(live.url + "/app")
    anon.wait_for_url("**/login?next=%2Fapp")
    pg = sign_in(live, browser_page, email)
    pg.click("#signout")
    pg.wait_for_url("**/login")
    pg.goto(live.url + "/app")
    pg.wait_for_url("**/login?next=%2Fapp")
