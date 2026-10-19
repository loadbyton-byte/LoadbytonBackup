"""Headless-browser smoke test of the dashboard. Skipped when Playwright/Chromium aren't available."""
import os
import socket
import threading
import time
from datetime import datetime, timedelta

import pytest

pw = pytest.importorskip("playwright.sync_api")

from leadengine.api import create_app  # noqa: E402
from leadengine.config import Settings  # noqa: E402
from leadengine.db import DB  # noqa: E402
from leadengine.demo import demo_signals  # noqa: E402
from leadengine.inventory import import_projects_csv  # noqa: E402
from leadengine.models import Signal  # noqa: E402
from leadengine.roi import record_spend  # noqa: E402
from leadengine.viewings import schedule  # noqa: E402

XSS = '<img src=x onerror="window.__xss=1">'
CSV = ("name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan\n"
       "Marina Edge,Acme,Dubai Marina,Dubai,1.2M,2.4M,\"1-3\",2030,60/40\n")


@pytest.fixture(scope="module")
def base_url():
    import uvicorn
    app = create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:"))
    p = app.state.pipeline
    for a in ("Layla", "Omar"):
        p.distribution.add_agent(a, languages=("en", "ar"), areas=("Dubai Marina",))
    p.ingest_many(demo_signals())
    xss = p.ingest(Signal(platform="facebook", external_id="xss1", source_type="lead_form", author_name=XSS, phone="0505550000",
                          text="Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week")).lead_id
    import_projects_csv(p.db, CSV)
    record_spend(p.db, "reddit", "oct", "2026-10", 500)
    schedule(p.db, xss, datetime.now() + timedelta(days=1), location="Marina Gate")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture()
def page(base_url):
    with pw.sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium"
        try:
            browser = p.chromium.launch(executable_path=exe) if os.path.exists(exe) else p.chromium.launch()
        except Exception as e:  # no browser installed
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.problems = []
        pg.on("console", lambda m: pg.problems.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: pg.problems.append(str(e)))
        pg.on("response", lambda r: pg.problems.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(base_url + "/")
        pg.wait_for_selector("#rows tr.row")
        yield pg
        browser.close()


def test_brand_loads_and_no_problems(page):
    assert page.eval_on_selector("header .brand img", "i => i.complete && i.naturalWidth > 0")
    assert page.title() == "DigitalBurj Leads"
    assert page.problems == []


def test_every_tab_renders(page):
    expect = {"agents": "Layla", "tasks": "First response", "roi": "reddit", "inventory": "Marina Edge"}
    for tab, text in expect.items():
        page.click(f"nav.tabs [data-tab={tab}]")
        page.wait_for_selector(f"#tab-{tab}:not([hidden]) >> text={text}", timeout=5000)
        assert page.get_attribute(f"nav.tabs [data-tab={tab}]", "aria-selected") == "true"
    page.click("nav.tabs [data-tab=leads]")
    assert page.problems == []


def test_xss_name_is_text_not_markup(page):
    page.wait_for_selector("#rows >> text=onerror")
    assert page.evaluate("window.__xss") is None
    assert page.locator("#rows img").count() == 0
    page.locator("#rows tr.row", has_text="onerror").click()
    page.wait_for_selector("#dlg[open]")
    assert page.evaluate("window.__xss") is None and page.locator("#dlg img").count() == 0


def test_lead_dialog_panels(page):
    page.locator("#rows tr.row").first.click()
    page.wait_for_selector("#dlg[open]")
    for sel in ("#nba", "#matches", "#dealsheet", "#viewform"):
        page.wait_for_selector(f"#dlg {sel}", state="attached", timeout=5000)
    page.click("#dlg [data-act=dealsheet]")
    page.wait_for_selector("#dlg #dealsheet >> text=Cash needed", timeout=5000)
    assert page.problems == []


def test_arabic_toggle_sets_rtl_and_back(page):
    assert page.get_attribute("html", "dir") in (None, "ltr")
    page.click("#lang")
    assert page.get_attribute("html", "dir") == "rtl" and page.get_attribute("html", "lang") == "ar"
    assert "العملاء" in page.inner_text("nav.tabs")
    page.click("#lang")
    assert page.get_attribute("html", "dir") == "ltr"
    assert page.problems == []


def test_phone_width_has_no_horizontal_scroll(page):
    page.set_viewport_size({"width": 375, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
