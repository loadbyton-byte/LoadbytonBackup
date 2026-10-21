import re

from leadengine.platform import site


def test_render_with_base_escapes_and_sets_direction():
    out = site.render_with_base("pub_error", "pub_base", "ar", {"title": "x<", "description": "<b>y</b>", "agency": "<i>Acme</i>", "message": "", "base_url": "http://x"})
    assert 'dir="rtl"' in out and 'lang="ar"' in out and "&lt;b&gt;y&lt;/b&gt;" in out and "<script" not in out and "&lt;i&gt;Acme" in out
    en = site.render_with_base("pub_error", "pub_base", "en", {"title": "t", "description": "d", "agency": "A", "prefix": "/t/pk_x", "canonical_path": "/p/costs", "base_url": "http://x"})
    assert 'dir="ltr"' in en and 'href="http://x/t/pk_x/p/costs?lang=en"' in en and 'href="/t/pk_x/p/costs?lang=ar"' in en and 'rel="canonical"' in en


def test_pub_strings_have_parity_and_every_template_key_exists():
    en, ar = site.strings("en"), site.strings("ar")
    assert set(en) == set(ar) and all(v.strip() for v in en.values()) and all(v.strip() for v in ar.values())
    names = [p.stem for p in (site.SITE_DIR / "templates").glob("pub_*.html")]
    assert {"pub_base", "pub_index", "pub_calc", "pub_result", "pub_thanks", "pub_concierge", "pub_error"} <= set(names)
    for n in names:
        assert site.template_keys(n) <= set(en), (n, site.template_keys(n) - set(en))
    from leadengine.capture import CONSENT_TEXT
    assert en["pub.consent"] == CONSENT_TEXT                                     # the English wording shown is exactly the wording stored as evidence


def test_pub_css_has_no_external_urls_and_uses_logical_properties():
    css = (site.SITE_DIR / "static" / "pub.css").read_text(encoding="utf-8")
    assert "http" not in css and "@import" not in css
    assert "margin-inline" in css or "inset-inline" in css
    assert not re.search(r"(?<![-\w])(margin|padding)-(left|right)", css) and "text-align:left" not in css.replace(" ", "")
