"""Headless-browser checks of the Markets page. Skipped without Playwright/Chromium."""
import pytest

pytest.importorskip("playwright.sync_api")

from datetime import datetime, timedelta, timezone  # noqa: E402

from leadengine.markets.cities import resolve  # noqa: E402

from .test_dashboard_browser import XSS, link_token, sign_in, workspace  # noqa: E402,F401


def seed_origins(live, c, rows):
    tid = c.get("/auth/me").json()["workspace"]["id"]
    db = live.app.state.tenants.db_for(tid)
    now = datetime.now(timezone.utc)
    with db.tx() as cx:
        for i, (country, city, tier, status, name) in enumerate(rows):
            when = (now - timedelta(days=1 + i % 5)).isoformat()
            cx.execute("INSERT INTO leads(display_name,first_seen,last_seen,platforms,tier,status,score,origin_country,origin_city,origin_basis) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (name, when, when, "[]", tier, status, 60, country, city, "phone"))


def standard_rows():
    gb, lon, inn, mum = "GB", resolve("London").id, "IN", resolve("Mumbai").id
    return ([(gb, lon, "HOT", "won", "Lead")] * 2 + [(gb, lon, "WARM", "new", "Lead")] * 6 + [(gb, lon, "NURTURE", "new", "Lead")] * 4
            + [(inn, mum, "WARM", "new", "Lead")] * 3 + [("AE", resolve("Dubai").id, "HOT", "new", "Lead")] * 5
            + [(gb, lon, "NURTURE", "new", XSS)])


def test_markets_page_renders_tables_and_focus(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/markets']")
    pg.wait_for_selector("#mk-table tbody tr")
    assert pg.inner_text("[data-kpi=top-country]") == "United Kingdom"
    assert pg.inner_text("[data-kpi=known]") == "100%"
    gb = pg.locator("tr.mk-country[data-id=GB]")
    assert gb.locator("td").nth(1).inner_text() == "13"
    gb.locator(".mk-toggle").click()
    pg.wait_for_selector("tr.mk-city")
    assert "London" in pg.inner_text("tr.mk-city")
    assert gb.locator(".mk-toggle").get_attribute("aria-expanded") == "true"
    gb.locator(".mk-toggle").click()
    assert pg.locator("tr.mk-city").count() == 0 and gb.locator(".mk-toggle").get_attribute("aria-expanded") == "false"
    focus = pg.inner_text("#mk-body")
    assert "measured · 13 leads" in focus and "editorial" in focus
    assert pg.inner_text("[data-kpi=top-focus]") == "United Kingdom"
    assert pg.locator("#mk-untapped tr").count() > 0 and "UAE" in pg.inner_text("#mk-untapped")
    assert pg.evaluate("window.__xss") is None and pg.problems == []


def test_ae_is_not_in_the_focus_list(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email)
    pg.goto(live.url + "/app#/markets")
    pg.wait_for_selector("#mk-table tbody tr")
    focus_card = pg.locator("#mk-body .card", has=pg.locator("h2", has_text="Where to focus next"))
    assert "United Arab Emirates" not in focus_card.inner_text() and "United Kingdom" in focus_card.inner_text()


def test_period_filter_and_empty_state(live, browser_page):
    email, c, h = workspace(live, seed=False)
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/markets']")
    pg.wait_for_selector("#mk-body .empty")
    assert "No lead origins yet" in pg.inner_text("#mk-body") and "phone country code" in pg.inner_text("#mk-body")
    seed_origins(live, c, standard_rows())
    pg.fill("#mk-period", "1999-01")
    pg.press("#mk-period", "Enter")
    pg.wait_for_selector("#mk-body .empty")
    pg.fill("#mk-period", "")
    pg.press("#mk-period", "Enter")
    pg.wait_for_selector("#mk-table tbody tr")


def test_spend_form_tags_a_market(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/roi']")
    pg.wait_for_selector("#spend-country option:nth-child(5)", state="attached")
    pg.fill("#spend-form input[name=source]", "reddit")
    pg.fill("#spend-form input[name=period]", "2026-10")
    pg.fill("#spend-city", "London")
    pg.fill("#spend-form input[name=amount_aed]", "1300")
    pg.click("#spend-form button[type=submit]")
    for _ in range(40):                                                                   # wait for the spend to be stored
        if c.get("/markets/intel", headers=h).json()["unallocated_spend_aed"] == 0 and any(x["spend_aed"] == 1300 for x in c.get("/markets/intel", headers=h).json()["cities"]):
            break
        pg.wait_for_timeout(50)
    pg.click(".side nav a[href='#/markets']")
    pg.wait_for_selector("#mk-table tbody tr")
    gbrow = pg.locator("tr.mk-country[data-id=GB]")
    assert "1,300" in gbrow.inner_text()
    gbrow.locator(".mk-toggle").click()
    pg.wait_for_selector("tr.mk-city")
    assert "1,300" in pg.locator("tr.mk-city[data-id='" + resolve("London").id + "']").inner_text()
    assert pg.problems == []


def test_markets_rtl_phone_width_and_agent_has_no_page(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email, width=375, height=800)
    pg.goto(live.url + "/app#/markets")
    pg.wait_for_selector("#mk-table tbody tr")
    pg.click("#lang-btn")
    pg.wait_for_selector("#view h1:has-text('الأسواق')")
    assert pg.evaluate("document.documentElement.dir") == "rtl"
    assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    # an agent never gets the page
    c.post("/auth/invite", json={"email": "agentm@agency.ae", "role": "agent"}, headers=h)
    a = browser_page()
    a.goto(link_token(live.mailer, "invite") and live.url + "/invite?token=" + link_token(live.mailer, "invite"))
    a.fill("input[name=name]", "Ag")
    a.fill("input[name=password]", "correct horse battery")
    a.click("button[type=submit]")
    a.wait_for_url("**/login")
    a.fill("input[name=email]", "agentm@agency.ae")
    a.fill("input[name=password]", "correct horse battery")
    a.press("input[name=password]", "Enter")
    a.wait_for_url("**/app")
    a.wait_for_selector("#view h1")
    assert not a.is_visible(".side nav a[href='#/markets']")
    a.goto(live.url + "/app#/markets")
    a.wait_for_selector("#view h1")
    assert "Markets" not in a.inner_text("#view h1")


def test_expanding_still_works_after_revisiting_the_page_and_by_keyboard(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email)
    for _ in range(4):                                                                    # each visit used to stack another click listener
        pg.click(".side nav a[href='#/markets']")
        pg.wait_for_selector("#mk-table tbody tr")
        pg.locator("tr.mk-country[data-id=GB] .mk-toggle").click()
        pg.wait_for_selector("tr.mk-city")
        assert pg.locator("tr.mk-city").count() >= 1
        pg.click(".side nav a[href='#/roi']")
        pg.wait_for_selector("#spend-form")
    pg.click(".side nav a[href='#/markets']")
    pg.wait_for_selector("#mk-table tbody tr")
    toggle = pg.locator("tr.mk-country[data-id=GB] .mk-toggle")
    toggle.focus()
    pg.keyboard.press("Enter")
    pg.wait_for_selector("tr.mk-city")
    assert toggle.get_attribute("aria-expanded") == "true"


def test_period_input_validates_and_sends_one_request(live, browser_page):
    email, c, h = workspace(live, seed=False)
    seed_origins(live, c, standard_rows())
    pg = sign_in(live, browser_page, email)
    pg.click(".side nav a[href='#/markets']")
    pg.wait_for_selector("#mk-table tbody tr")
    seen = []
    pg.on("request", lambda r: seen.append(r.url) if "/markets/intel?" in r.url else None)
    pg.fill("#mk-period", "Oct")
    pg.press("#mk-period", "Enter")
    pg.wait_for_selector("#mk-body .err")
    assert "2026-10" in pg.inner_text("#mk-body") and seen == []                         # rejected on the client, no request
    pg.fill("#mk-period", "2026-13")
    pg.press("#mk-period", "Enter")
    assert seen == []
