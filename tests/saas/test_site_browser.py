"""Headless-browser checks of the public site and the auth pages. Skipped without Playwright/Chromium."""
import re

import pytest

pytest.importorskip("playwright.sync_api")

from .conftest import PW  # noqa: E402


def mail_link(live, kind):
    for m in reversed(live.mailer.outbox):
        hit = re.search(r"(/%s\?token=[A-Za-z0-9_-]+)" % kind, m["text"])
        if hit:
            return live.url + hit.group(1)
    raise AssertionError(kind)


def test_register_verify_login_logout_by_keyboard_only(live, browser_page):
    pg = browser_page()
    pg.goto(live.url + "/start")
    pg.fill("input[name=email]", "kb@agency.ae")
    pg.fill("input[name=password]", PW)
    pg.fill("input[name=name]", "Kay")
    pg.fill("input[name=agency_name]", "Keyboard Realty")
    pg.press("input[name=agency_name]", "Enter")                         # submit without a mouse
    pg.wait_for_url("**/start?sent=1")
    assert "confirm" in pg.inner_text("main").lower()
    pg.goto(mail_link(live, "verify"))
    pg.click("button[type=submit]")
    pg.wait_for_selector(".okmsg")
    pg.goto(live.url + "/login")
    pg.fill("input[name=email]", "kb@agency.ae")
    pg.fill("input[name=password]", PW)
    pg.press("input[name=password]", "Enter")
    pg.wait_for_url("**/app")
    assert [c for c in pg.context.cookies() if c["name"] == "dbl_session" and c["httpOnly"]]
    assert not [p for p in pg.problems if "404" not in p and not p.endswith("/app")]       # /app itself lands in Task 12


def test_forgot_then_reset_in_the_browser(live, browser_page):
    pg = browser_page()
    pg.goto(live.url + "/start")
    for n, v in (("email", "fr@agency.ae"), ("password", PW), ("name", "Fay"), ("agency_name", "Forgot Realty")):
        pg.fill(f"input[name={n}]", v)
    pg.click("button[type=submit]")
    pg.wait_for_url("**/start?sent=1")
    pg.goto(live.url + "/forgot")
    pg.fill("input[name=email]", "fr@agency.ae")
    pg.click("button[type=submit]")
    pg.wait_for_url("**/forgot?sent=1")
    pg.goto(mail_link(live, "reset"))
    pg.fill("input[name=password]", "a brand new passphrase")
    pg.click("button[type=submit]")
    pg.wait_for_url("**/login?reset=1")
    assert "changed" in pg.inner_text("main")


def test_rtl_layout_and_no_horizontal_scroll_on_phone(live, browser_page):
    pg = browser_page(width=375, height=800, locale="ar-AE")
    for path in ("/", "/features", "/pricing", "/faq", "/login", "/start"):
        pg.goto(live.url + path)
        assert pg.evaluate("document.documentElement.dir") == "rtl", path
        assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), path
    assert pg.problems == []


def test_home_desktop_no_problems_and_assets_load(live, browser_page):
    pg = browser_page()
    pg.goto(live.url + "/")
    assert pg.evaluate("[...document.images].every(i => i.complete && i.naturalWidth > 0)")
    assert pg.inner_text("h1")
    pg.click("a.lang")
    pg.wait_for_url(live.url + "/")
    assert pg.evaluate("document.documentElement.dir") == "rtl"
    assert pg.problems == []


def test_mobile_menu_opens_without_javascript(live, browser_page):
    pg = browser_page(width=375, height=800)
    pg.goto(live.url + "/")
    assert not pg.is_visible("nav[aria-label=Main] a[href='/features']")
    pg.click("details.menu summary")
    assert pg.is_visible("nav[aria-label=Main] a[href='/features']")
