import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from leadengine.platform import site

from .test_app_auth import make_app

PENDING_LINKS = {"/start", "/login", "/app"}          # pages built in Task 11 (/start /login ...) are listed here only until that task lands


@pytest.fixture()
def client(tmp_path):
    app, _ = make_app(tmp_path, contact_email="hello@example.ae")
    return TestClient(app)


PAGES = list(site.PAGES.items())


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("name,spec", PAGES)
def test_every_page_renders_in_both_languages(client, name, spec, lang):
    path = spec[0]
    r = client.get(path, headers={"Accept-Language": lang})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    t = r.text
    assert f'<html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}">' in t
    assert len(re.findall(r"<h1[ >]", t)) == 1
    title = re.search(r"<title>(.*?)</title>", t).group(1)
    assert 0 < len(title) <= 70
    assert re.search(r'<meta name="description" content="[^"]{20,}"', t)
    assert 'rel="canonical"' in t and 'hreflang="en"' in t and 'hreflang="ar"' in t and 'hreflang="x-default"' in t
    for og in ("og:title", "og:description", "og:image", "og:url", "og:type"):
        assert f'property="{og}"' in t
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', t, re.S).group(1)
    assert json.loads(ld)["@graph"]
    assert "Content-Security-Policy" in r.headers and "x-content-type-options" in r.headers


def test_i18n_keys_have_parity():
    en, ar = site.strings("en"), site.strings("ar")
    assert set(en) == set(ar)
    assert all(isinstance(v, str) and v.strip() for v in en.values()) and all(v.strip() for v in ar.values())
    names = [p.stem for p in (site.SITE_DIR / "templates").glob("*.html")]
    for n in names:
        missing = site.template_keys(n) - set(en)
        assert not missing, (n, missing)


def test_dynamic_values_are_escaped():
    out = site.render_page("pricing", "en", {"base_url": "http://x", "price": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in out and "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_pricing_comes_from_json_and_defaults_to_contact_us(tmp_path):
    assert site.pricing_text("en") == site.strings("en")["pricing.default_price"]
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"price_text": {"en": "AED 499 / month", "ar": ""}}))
    assert site.pricing_text("en", p) == "AED 499 / month" and site.pricing_text("ar", p) == site.strings("ar")["pricing.default_price"]
    p.write_text("not json")
    assert site.pricing_text("en", p) == site.strings("en")["pricing.default_price"]
    assert "contact us" in site.render_page("pricing", "en", {"base_url": "http://x"}).lower()


def test_no_invented_claims():
    blob = " ".join(p.read_text(encoding="utf-8") for p in (site.SITE_DIR / "templates").glob("*.html"))
    blob += " ".join(site.strings("en").values()) + " " + " ".join(site.strings("ar").values())
    low = blob.lower()
    for banned in ("testimonial", "trusted by", "as seen", "best-in-class", "world-class", "#1", "number one", "guaranteed", "award"):
        assert banned not in low, banned
    assert not re.search(r"\d[\d,.]*\s*\+?\s*(agencies|customers|clients|brokers|users)", low)


def test_legal_pages_carry_counsel_notice(client):
    for path in ("/privacy", "/terms"):
        assert "counsel" in client.get(path).text.lower()
        assert "المستشار" in client.get(path, headers={"Accept-Language": "ar"}).text or "مستشار" in client.get(path, headers={"Accept-Language": "ar"}).text


def test_sitemap_lists_public_pages_only(client):
    x = client.get("/sitemap.xml").text
    for p, _ in site.PAGES.values():
        assert f"http://testserver{p}</loc>" in x or f"{p}</loc>" in x
    for private in ("/app", "/login", "/start", "/auth", "/leads"):
        assert private not in x
    assert "Disallow: /app" in client.get("/robots.txt").text


def test_internal_links_resolve(client):
    seen = set()
    for _, (path, _) in PAGES:
        for lang in ("en", "ar"):
            for href in re.findall(r'href="(/[^"#]*)"', client.get(path, headers={"Accept-Language": lang}).text):
                href = href.split("?")[0]
                if href in seen or href in PENDING_LINKS or href.startswith("/lang/"):
                    continue
                seen.add(href)
                assert client.get(href, follow_redirects=False).status_code in (200, 301, 302, 303, 307), href
    assert {"/features", "/pricing", "/privacy", "/terms"} <= seen


def test_lang_cookie_switch(client):
    r = client.get("/lang/ar?next=/pricing", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pricing" and "dbl_lang=ar" in r.headers["set-cookie"]
    assert 'dir="rtl"' in client.get("/pricing").text
    client.get("/lang/en?next=/pricing")
    assert 'dir="ltr"' in client.get("/pricing", headers={"Accept-Language": "ar"}).text         # cookie beats Accept-Language
    assert 'dir="rtl"' in client.get("/pricing?lang=ar").text                                    # explicit query beats cookie
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)", "/a:b"):
        assert client.get("/lang/ar", params={"next": evil}, follow_redirects=False).headers["location"] == "/"
    assert client.get("/lang/fr", follow_redirects=False).status_code == 404


def test_detect_lang():
    assert site.detect_lang("ar-AE,ar;q=0.9,en;q=0.5", None) == "ar"
    assert site.detect_lang("en-US,en;q=0.9,ar;q=0.8", None) == "en"
    assert site.detect_lang("fr", None) == "en" and site.detect_lang(None, "ar") == "ar" and site.detect_lang("ar", "en") == "en"
    assert site.detect_lang("ar;q=junk", None) == "en"


def test_contact_email_is_escaped_and_optional(tmp_path):
    app, _ = make_app(tmp_path, contact_email='"><script>x</script>@a.ae')
    assert "<script>x" not in TestClient(app).get("/contact").text
    app, _ = make_app(tmp_path)
    assert "mailto:" not in TestClient(app).get("/contact").text
    app, _ = make_app(tmp_path, contact_email="hello@example.ae")
    assert 'href="mailto:hello@example.ae"' in TestClient(app).get("/contact").text


def test_brand_assets_referenced_by_site_exist(client):
    for p in ("/static/brand/wordmark.webp", "/static/brand/real-estate.webp", "/static/brand/opengraph.png", "/static/brand/favicon.svg",
              "/site-static/site.css", "/static/fonts/plus-jakarta-sans-latin-wght-normal.woff2"):
        assert client.get(p).status_code == 200, p
